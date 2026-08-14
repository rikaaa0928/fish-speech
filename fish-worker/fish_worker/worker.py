from __future__ import annotations

import asyncio
import contextlib
import os
import shlex
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import aiohttp
import websockets

from fish_worker import __version__
from fish_worker.api_client import call_api_server
from fish_worker.config import Config, env_int, normalized_subprocess_env
from fish_worker.health import GPU_FATAL_EXIT_CODE, HealthCheck, detect_gpu, run_cuda_tiny_op
from fish_worker.logging_utils import log
from fish_worker.payload import api_server_payload
from fish_worker.priority import PRIORITIES, Priority, effective_queue_length, priority_map_to_wire
from fish_worker.protocol import (
    append_token,
    redact_query_token,
    send_error,
    send_msg,
    unpack_msg,
)
from fish_worker.reference_cache import ensure_api_reference


@dataclass
class QueuedRequest:
    request_id: str
    priority: Priority
    input_chars: int
    future: asyncio.Future[bool]


class Worker:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.local_inflight = 0
        self.active_requests: dict[str, tuple[Priority, int]] = {}
        self.priority_queues: dict[Priority, list[QueuedRequest]] = {
            priority: [] for priority in PRIORITIES
        }
        self.api_server_reject_count = 0
        self.api_reference_ids: set[str] = set()
        self.api_reference_locks: dict[str, asyncio.Lock] = {}
        self.capacity_condition = asyncio.Condition()
        self.ewma_latency_ms: float | None = None
        self.last_error: str | None = None
        self.last_api_server_healthy: bool | None = None
        self.last_api_server_health_detail: str | None = None
        self.last_api_server_health_checked_at: float | None = None
        self.last_cuda_probe_checked_at: float | None = None
        self.last_cuda_probe_result: tuple[bool, str | None] | None = None
        self.gpu_watchdog_failures = 0
        self.api_server_watchdog_failures = 0
        self.last_api_server_restart_at: float | None = None
        self.api_server_restart_lock = asyncio.Lock()
        self.started_at = datetime.now(timezone.utc)
        self.stop_event = asyncio.Event()
        self.api_server_process: asyncio.subprocess.Process | None = None

        # Metrics for monitoring
        self.total_completed_tasks = 0
        self.total_completed_chars = 0
        self.total_processing_time_sec = 0.0

    @property
    def local_queued(self) -> int:
        return sum(len(queue) for queue in self.priority_queues.values())

    def queued_by_priority(self) -> dict[Priority, int]:
        return {priority: len(self.priority_queues[priority]) for priority in PRIORITIES}

    def queued_chars_by_priority(self) -> dict[Priority, int]:
        return {
            priority: sum(request.input_chars for request in self.priority_queues[priority])
            for priority in PRIORITIES
        }

    def inflight_by_priority(self) -> dict[Priority, int]:
        return {
            priority: sum(1 for active_priority, _ in self.active_requests.values() if active_priority is priority)
            for priority in PRIORITIES
        }

    def inflight_chars_by_priority(self) -> dict[Priority, int]:
        return {
            priority: sum(
                input_chars
                for active_priority, input_chars in self.active_requests.values()
                if active_priority is priority
            )
            for priority in PRIORITIES
        }

    def max_queue_for_priority(self, priority: Priority) -> int:
        return self.config.api_server_max_queued_requests_by_priority.get(
            priority,
            self.config.api_server_max_queued_requests,
        )

    def effective_queued_for_priority(self, priority: Priority) -> int:
        return effective_queue_length(priority, self.queued_by_priority())

    def can_start_immediately(self, priority: Priority) -> bool:
        if self.local_inflight >= self.config.api_server_max_running_requests:
            return False
        return not any(self.priority_queues[candidate] for candidate in PRIORITIES[: priority.index + 1])

    async def run(self) -> None:
        self.config.cache_dir.mkdir(parents=True, exist_ok=True)
        self.config.api_server_references_dir.mkdir(parents=True, exist_ok=True)

        if self.config.manage_api_server:
            self.api_server_process = await self.start_api_server()

        await self.wait_api_server_ready()

        while not self.stop_event.is_set():
            try:
                await self.connect_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)
                log("worker connection failed", error=str(exc))
                await asyncio.sleep(5)

    async def start_api_server(self) -> asyncio.subprocess.Process:
        self.last_api_server_restart_at = time.monotonic()
        cmd = os.getenv("API_SERVER_COMMAND")
        if cmd:
            args = ["bash", "-lc", cmd]
        else:
            args = [
                sys.executable,
                "-m",
                "tools.api_server",
                "--listen",
                f"{self.config.api_server_host}:{self.config.api_server_port}",
                "--llama-checkpoint-path",
                str(self.config.model_dir),
                "--decoder-checkpoint-path",
                str(self.config.api_server_decoder_checkpoint_path),
                "--decoder-config-name",
                self.config.api_server_decoder_config_name,
                "--decoder-dtype",
                self.config.api_server_decoder_dtype,
                "--workers",
                str(self.config.api_server_workers),
                "--max-text-length",
                str(self.config.api_server_max_text_length),
                "--speed-method",
                self.config.api_server_speed_method,
            ]
            if self.config.api_server_llama_max_seq_len is not None:
                args.append("--llama-max-seq-len")
                args.append(str(self.config.api_server_llama_max_seq_len))
            if self.config.api_server_compile:
                args.append("--compile")
            if self.config.api_server_half:
                args.append("--half")
            extra_args = os.getenv("API_SERVER_EXTRA_ARGS")
            if extra_args:
                args.extend(shlex.split(extra_args))

        log("starting Fish API server", command=" ".join(args))
        return await asyncio.create_subprocess_exec(*args, env=normalized_subprocess_env())

    async def wait_api_server_ready(self) -> None:
        deadline = time.monotonic() + env_int("API_SERVER_STARTUP_TIMEOUT_SECONDS", 900)
        while time.monotonic() < deadline:
            health = await self.deep_health_check(force_cuda_probe=True)
            if health.healthy:
                log("Fish API server is healthy", api_server_url=self.config.api_server_url)
                self.last_api_server_healthy = True
                self.last_api_server_health_detail = None
                return
            self.last_api_server_health_detail = health.detail
            await asyncio.sleep(2)
        raise RuntimeError(
            "Fish API server did not become healthy before timeout"
            + (f": {self.last_api_server_health_detail}" if self.last_api_server_health_detail else "")
        )

    async def connect_once(self) -> None:
        url = append_token(self.config.manager_url, self.config.worker_token)
        log("connecting to manager", manager_url=redact_query_token(self.config.manager_url), worker_id=self.config.worker_id)
        async with websockets.connect(url, max_size=None, ping_interval=20, ping_timeout=20) as ws:
            log("connected to manager", worker_id=self.config.worker_id)
            await send_msg(ws, "worker_hello", self.worker_hello())

            heartbeat_task = asyncio.create_task(self.heartbeat_loop(ws))
            reader_task = asyncio.create_task(self.manager_reader_loop(ws))
            try:
                done, _ = await asyncio.wait(
                    {heartbeat_task, reader_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in done:
                    exc = task.exception()
                    if exc is not None:
                        raise exc
                log("manager connection ended", worker_id=self.config.worker_id)
            finally:
                for task in (heartbeat_task, reader_task):
                    if not task.done():
                        task.cancel()
                for task in (heartbeat_task, reader_task):
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task

    async def manager_reader_loop(self, ws: Any) -> None:
        async for raw in ws:
            if isinstance(raw, str):
                continue
            message = unpack_msg(raw)
            msg_type = message.get("type")
            if msg_type == "inference_request":
                data = message["data"]
                asyncio.create_task(self.handle_inference(ws, data))
            elif msg_type == "cancel_request":
                log("cancel requested", data=message.get("data"))
            elif msg_type == "restart_api_server":
                reason = message.get("data", {}).get("reason", "manager request")
                log("manager requested API server restart", reason=reason)
                asyncio.create_task(self.restart_api_server(reason=reason, force=True))
            elif msg_type == "restart_worker":
                reason = message.get("data", {}).get("reason", "manager request")
                log("manager requested worker process restart", reason=reason)
                import sys
                sys.exit(0)

    def worker_hello(self) -> dict[str, Any]:
        gpu = detect_gpu()
        queue_limits = priority_map_to_wire(self.config.api_server_max_queued_requests_by_priority)
        return {
            "worker_id": self.config.worker_id,
            "version": __version__,
            "model_id": self.config.model_id,
            "model_revision": os.getenv("MODEL_REVISION"),
            "gpu_name": gpu.get("gpu_name"),
            "gpu_count": gpu.get("gpu_count", 0),
            "vram_total_mb": gpu.get("vram_total_mb"),
            "max_running_requests": self.config.api_server_max_running_requests,
            "max_queued_requests": self.config.api_server_max_queued_requests,
            "worker_max_inflight": self.config.api_server_max_running_requests,
            "worker_max_queue": self.config.api_server_max_queued_requests,
            "max_queued_requests_by_priority": queue_limits,
            "sglang_url": self.config.api_server_url,
            "started_at": self.started_at.isoformat().replace("+00:00", "Z"),
        }

    async def heartbeat_loop(self, ws: Any) -> None:
        while True:
            health = await self.deep_health_check()
            self.log_api_server_health_change(health.healthy, health.detail)
            await self.apply_health_watchdog(health)
            await send_msg(
                ws,
                "heartbeat",
                {
                    "worker_id": self.config.worker_id,
                    "workload_metrics_version": 1,
                    "ready": health.healthy,
                    "sglang_healthy": health.healthy,
                    "inflight": self.local_inflight,
                    "queued": self.local_queued,
                    "queued_by_priority": priority_map_to_wire(self.queued_by_priority()),
                    "queued_chars_by_priority": priority_map_to_wire(
                        self.queued_chars_by_priority()
                    ),
                    "inflight_by_priority": priority_map_to_wire(self.inflight_by_priority()),
                    "inflight_chars_by_priority": priority_map_to_wire(
                        self.inflight_chars_by_priority()
                    ),
                    "max_running_requests": self.config.api_server_max_running_requests,
                    "max_queued_requests": self.config.api_server_max_queued_requests,
                    "max_queued_requests_by_priority": priority_map_to_wire(
                        self.config.api_server_max_queued_requests_by_priority
                    ),
                    "vram_used_mb": health.gpu.get("vram_used_mb"),
                    "vram_free_mb": health.gpu.get("vram_free_mb"),
                    "gpu_utilization_percent": health.gpu.get("gpu_utilization_percent"),
                    "ewma_latency_ms": self.ewma_latency_ms,
                    "last_error": self.last_error,
                    "total_completed_tasks": self.total_completed_tasks,
                    "total_completed_chars": self.total_completed_chars,
                    "total_processing_time_sec": self.total_processing_time_sec,
                },
            )
            await asyncio.sleep(self.config.heartbeat_interval_seconds)

    def log_api_server_health_change(self, healthy: bool, detail: str | None) -> None:
        if healthy:
            self.last_error = None
        else:
            self.last_error = detail or "Fish API server health check failed"

        if self.last_api_server_healthy == healthy and self.last_api_server_health_detail == detail:
            return

        log(
            "Fish API server health changed",
            healthy=healthy,
            detail=detail,
            api_server_url=self.config.api_server_url,
        )
        self.last_api_server_healthy = healthy
        self.last_api_server_health_detail = detail

    async def api_server_healthy(self) -> bool:
        return (await self.deep_health_check()).healthy

    async def deep_health_check(self, *, force_cuda_probe: bool = False) -> HealthCheck:
        gpu = detect_gpu()
        if self.config.require_gpu and gpu.get("gpu_count", 0) < 1:
            detail = "gpu_unavailable: nvidia-smi found no usable GPU"
            if gpu.get("error"):
                detail = f"{detail}: {gpu['error']}"
            return HealthCheck(False, detail, gpu, gpu_failure=True)

        healthy, detail = await self.api_server_health_for_heartbeat()
        if not healthy:
            return HealthCheck(False, detail, gpu, api_server_failure=True)

        if self.config.require_gpu:
            cuda_ok, cuda_detail = await self.cuda_tiny_op_health(force=force_cuda_probe)
            if not cuda_ok:
                return HealthCheck(False, f"cuda_unavailable: {cuda_detail}", gpu, gpu_failure=True)

        return HealthCheck(True, None, gpu)

    async def api_server_health_for_heartbeat(self) -> tuple[bool, str | None]:
        process_error = self.api_server_process_error()
        if process_error is not None:
            return False, process_error

        if self.local_inflight > 0 and self.last_api_server_healthy is not None:
            return self.last_api_server_healthy, self.last_api_server_health_detail

        now = time.monotonic()
        if (
            self.local_inflight == 0
            and self.last_api_server_health_checked_at is not None
            and now - self.last_api_server_health_checked_at < self.config.heartbeat_interval_seconds
            and self.last_api_server_healthy is not None
        ):
            return self.last_api_server_healthy, self.last_api_server_health_detail

        return await self.api_server_health()

    def api_server_process_error(self) -> str | None:
        if not self.config.manage_api_server:
            return None
        if self.api_server_process is None:
            return "Fish API server process is not running"
        if self.api_server_process.returncode is not None:
            return f"Fish API server process exited with code {self.api_server_process.returncode}"
        return None

    async def api_server_health(self) -> tuple[bool, str | None]:
        self.last_api_server_health_checked_at = time.monotonic()
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(f"{self.config.api_server_url}/v1/health", timeout=3) as response:
                    if response.status == 200:
                        return True, None
                    return False, f"HTTP {response.status}"
            except Exception as exc:  # noqa: BLE001
                return False, f"{type(exc).__name__}: {exc}"

    async def cuda_tiny_op_health(self, *, force: bool = False) -> tuple[bool, str | None]:
        now = time.monotonic()
        if (
            not force
            and self.last_cuda_probe_result is not None
            and self.last_cuda_probe_checked_at is not None
            and now - self.last_cuda_probe_checked_at < self.config.cuda_probe_interval_seconds
        ):
            return self.last_cuda_probe_result

        result = await asyncio.to_thread(run_cuda_tiny_op, self.config.cuda_probe_timeout_seconds)
        self.last_cuda_probe_checked_at = now
        self.last_cuda_probe_result = result
        return result

    async def apply_health_watchdog(self, health: HealthCheck) -> None:
        if health.gpu_failure:
            self.gpu_watchdog_failures += 1
            log(
                "GPU watchdog failure",
                failures=self.gpu_watchdog_failures,
                threshold=self.config.gpu_watchdog_failures_before_exit,
                detail=health.detail,
            )
            if self.gpu_watchdog_failures >= self.config.gpu_watchdog_failures_before_exit:
                log("GPU watchdog exiting worker container", exit_code=GPU_FATAL_EXIT_CODE, detail=health.detail)
                raise SystemExit(GPU_FATAL_EXIT_CODE)
        else:
            self.gpu_watchdog_failures = 0

        if health.api_server_failure:
            await self.record_api_server_failure(health.detail or "Fish API server health check failed")
        elif health.healthy:
            self.api_server_watchdog_failures = 0

    async def record_api_server_failure(self, detail: str) -> None:
        now = time.monotonic()
        if (
            self.last_api_server_restart_at is not None
            and now - self.last_api_server_restart_at < 60
        ):
            return

        self.api_server_watchdog_failures += 1
        log(
            "Fish API server watchdog failure",
            failures=self.api_server_watchdog_failures,
            threshold=self.config.api_server_watchdog_failures_before_restart,
            detail=detail,
        )
        if self.api_server_watchdog_failures < self.config.api_server_watchdog_failures_before_restart:
            return
        if not self.config.manage_api_server:
            return
        await self.restart_api_server(reason=detail)

    async def restart_api_server(self, *, reason: str, force: bool = False) -> None:
        async with self.api_server_restart_lock:
            now = time.monotonic()
            if (
                not force
                and self.last_api_server_restart_at is not None
                and now - self.last_api_server_restart_at < self.config.api_server_restart_cooldown_seconds
            ):
                return

            self.last_api_server_restart_at = now
            self.api_server_watchdog_failures = 0
            self.last_api_server_healthy = False
            self.last_api_server_health_detail = f"restarting Fish API server: {reason}"
            self.last_cuda_probe_checked_at = None
            self.last_cuda_probe_result = None
            log("restarting Fish API server", reason=reason)
            await self.stop_api_server()
            self.api_server_process = await self.start_api_server()

    async def stop_api_server(self) -> None:
        process = self.api_server_process
        if process is None or process.returncode is not None:
            return

        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=20)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()

    async def acquire_request_slot(
        self,
        request_id: str,
        priority: Priority,
        input_chars: int,
    ) -> bool:
        async with self.capacity_condition:
            if self.can_start_immediately(priority):
                self.local_inflight += 1
                self.active_requests[request_id] = (priority, input_chars)
                return True

            effective_queued = self.effective_queued_for_priority(priority)
            max_queued = self.max_queue_for_priority(priority)
            if effective_queued >= max_queued:
                self.api_server_reject_count += 1
                log(
                    "worker rejected inference",
                    request_id=request_id,
                    priority=priority.value,
                    reason="overloaded",
                    inflight=self.local_inflight,
                    queued=self.local_queued,
                    queued_by_priority=priority_map_to_wire(self.queued_by_priority()),
                    effective_queued=effective_queued,
                    max_running=self.config.api_server_max_running_requests,
                    max_queued=max_queued,
                )
                return False

            future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
            self.priority_queues[priority].append(
                QueuedRequest(request_id, priority, input_chars, future)
            )
            log(
                "inference queued",
                request_id=request_id,
                priority=priority.value,
                inflight=self.local_inflight,
                queued=self.local_queued,
                queued_by_priority=priority_map_to_wire(self.queued_by_priority()),
                effective_queued=effective_queued + 1,
                max_queued=max_queued,
            )

        try:
            return await future
        except asyncio.CancelledError:
            await self.remove_queued_request(request_id, priority, future)
            raise

    async def remove_queued_request(
        self,
        request_id: str,
        priority: Priority,
        future: asyncio.Future[bool],
    ) -> None:
        async with self.capacity_condition:
            queue = self.priority_queues[priority]
            self.priority_queues[priority] = [
                item for item in queue if item.request_id != request_id or item.future is not future
            ]

    async def release_request_slot(self, request_id: str) -> None:
        async with self.capacity_condition:
            self.active_requests.pop(request_id, None)
            self.local_inflight = max(0, self.local_inflight - 1)
            self.start_queued_requests_locked()

    def start_queued_requests_locked(self) -> None:
        while self.local_inflight < self.config.api_server_max_running_requests:
            started = False
            for priority in PRIORITIES:
                queue = self.priority_queues[priority]
                while queue:
                    queued = queue.pop(0)
                    if queued.future.done():
                        continue

                    self.local_inflight += 1
                    self.active_requests[queued.request_id] = (
                        queued.priority,
                        queued.input_chars,
                    )
                    queued.future.set_result(True)
                    log(
                        "queued inference admitted",
                        request_id=queued.request_id,
                        priority=queued.priority.value,
                        inflight=self.local_inflight,
                        queued=self.local_queued,
                        queued_by_priority=priority_map_to_wire(self.queued_by_priority()),
                    )
                    started = True
                    break
                if started:
                    break
            if not started:
                break

    async def handle_inference(self, ws: Any, request: dict[str, Any]) -> None:
        request_id = request["request_id"]
        api_kind = request.get("api_kind") or "unknown"
        priority = Priority.parse(request.get("priority"))
        raw_payload = request.get("payload") or {}
        input_chars = len(raw_payload.get("text") or raw_payload.get("input") or "")
        started = time.monotonic()
        slot_acquired = await self.acquire_request_slot(request_id, priority, input_chars)
        if not slot_acquired:
            await send_error(ws, request_id, "overloaded", "worker overloaded", retryable=True)
            return

        try:
            payload = api_server_payload(dict(request.get("payload") or {}), stream=bool(request.get("stream")))
            input_text = payload.get("text") or ""
            if self.config.api_server_tts_max_new_tokens is not None:
                payload.setdefault("max_new_tokens", self.config.api_server_tts_max_new_tokens)
            refs = []
            request_references = request.get("references") or []
            api_reference_id: str | None = None
            metadata_references = 0
            inline_references = 0
            inline_audio_bytes = 0
            log(
                "inference request received",
                request_id=request_id,
                api_kind=api_kind,
                priority=priority.value,
                stream=bool(request.get("stream")),
                input_chars=len(input_text),
                references=len(request_references),
                inflight=self.local_inflight,
            )
            reference_started = time.monotonic()
            stored_references = [ref for ref in request_references if ref.get("voice_id") and ref.get("checksum")]
            can_use_reference_id = len(stored_references) == 1 and len(request_references) == 1
            for ref in request_references:
                content_type = ref.get("content_type") or "audio/wav"
                voice_id = ref.get("voice_id")
                checksum = ref.get("checksum")
                if voice_id and checksum:
                    metadata_references += 1
                    if can_use_reference_id:
                        api_reference_id, _ = await ensure_api_reference(self, ref, content_type)
                        continue

                    _, path = await ensure_api_reference(self, ref, content_type)
                    audio_bytes = path.read_bytes()
                else:
                    audio_bytes = ref.get("audio_bytes")
                    if audio_bytes is None:
                        raise ValueError("reference must include voice_id/checksum or audio_bytes")
                    inline_references += 1
                    inline_audio_bytes += len(audio_bytes)
                refs.append({"audio": audio_bytes, "text": ref["text"]})
            reference_ms = (time.monotonic() - reference_started) * 1000
            log(
                "inference references prepared",
                request_id=request_id,
                api_kind=api_kind,
                priority=priority.value,
                references=len(refs) + (1 if api_reference_id else 0),
                metadata_references=metadata_references,
                inline_references=inline_references,
                inline_audio_bytes=inline_audio_bytes,
                api_reference_id=api_reference_id,
                reference_ms=reference_ms,
            )

            if api_reference_id:
                payload["reference_id"] = api_reference_id
            if refs:
                payload["references"] = refs

            result = await call_api_server(self, ws, request_id, payload, stream=payload["streaming"])
            if result is not None:
                total_time_sec = time.monotonic() - started
                total_ms = total_time_sec * 1000
                timings = dict(result["timings"])
                timings["total_ms"] = total_ms
                timings["reference_ms"] = reference_ms
                
                # Update metrics
                self.total_completed_tasks += 1
                self.total_completed_chars += len(input_text)
                self.total_processing_time_sec += total_time_sec

                log(
                    "inference completed",
                    request_id=request_id,
                    api_kind=api_kind,
                    priority=priority.value,
                    stream=payload["streaming"],
                    input_chars=len(input_text),
                    references=len(refs) + (1 if api_reference_id else 0),
                    metadata_references=metadata_references,
                    inline_references=inline_references,
                    api_reference_id=api_reference_id,
                    audio_bytes=result["audio_bytes"],
                    chunks=result["chunks"],
                    total_ms=total_ms,
                    reference_ms=reference_ms,
                    api_server_ms=timings["api_server_ms"],
                    chunk_send_ms=timings["chunk_send_ms"],
                    finish_reason=result["finish_reason"],
                    generated_tokens=result["generated_tokens"],
                    max_new_tokens=result["max_new_tokens"],
                    error_code=result["error_code"],
                )
                await send_msg(
                    ws,
                    "inference_done",
                    {
                        "request_id": request_id,
                        "timings": timings,
                        "audio_bytes": result["audio_bytes"],
                        "chunks": result["chunks"],
                        "http_status": result["http_status"],
                        "finish_reason": result["finish_reason"],
                        "generated_tokens": result["generated_tokens"],
                        "max_new_tokens": result["max_new_tokens"],
                        "input_characters": result["input_characters"],
                        "error_code": result["error_code"],
                    },
                )
        except aiohttp.ClientError as exc:
            self.last_error = str(exc)
            await self.record_api_server_failure(f"{type(exc).__name__}: {exc}")
            log(
                "api server request failed",
                request_id=request_id,
                api_kind=api_kind,
                priority=priority.value,
                error=str(exc),
                total_ms=(time.monotonic() - started) * 1000,
            )
            await send_error(ws, request_id, "sglang_unavailable", str(exc), retryable=True)
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            log(
                "inference failed",
                request_id=request_id,
                api_kind=api_kind,
                priority=priority.value,
                error=str(exc),
                total_ms=(time.monotonic() - started) * 1000,
            )
            await send_error(ws, request_id, "inference_failed", str(exc), retryable=False)
        finally:
            elapsed_ms = (time.monotonic() - started) * 1000
            self.update_ewma(elapsed_ms)
            await self.release_request_slot(request_id)

    def update_ewma(self, elapsed_ms: float) -> None:
        if self.ewma_latency_ms is None:
            self.ewma_latency_ms = elapsed_ms
        else:
            self.ewma_latency_ms = self.ewma_latency_ms * 0.8 + elapsed_ms * 0.2
