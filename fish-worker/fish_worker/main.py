from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import signal
import shlex
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

import aiohttp
import msgpack
import websockets

from fish_worker import __version__


def log(message: str, **fields: Any) -> None:
    timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    parts = [timestamp, message]
    for key, value in fields.items():
        if isinstance(value, float):
            rendered = f"{value:.2f}"
        else:
            rendered = json.dumps(value, ensure_ascii=True, default=str)
        parts.append(f"{key}={rendered}")
    print(" ".join(parts), flush=True)


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return int(value)


def env_optional_int(name: str) -> int | None:
    value = os.getenv(name)
    if value is None or value == "":
        return None
    return int(value)


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value not in {"0", "false", "False", "no", "NO"}


def normalized_subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    omp_threads = env.get("OMP_NUM_THREADS", "")
    if not omp_threads.isdecimal() or int(omp_threads) < 1:
        env["OMP_NUM_THREADS"] = "1"
    env.setdefault("FISH_API_SERVER_ACCESS_LOG", "0")
    return env


@dataclass(frozen=True)
class Config:
    manager_url: str
    worker_token: str
    worker_id: str
    model_id: str
    model_dir: Path
    cache_dir: Path
    api_server_host: str
    api_server_port: int
    api_server_url_override: str | None
    api_server_max_running_requests: int
    api_server_max_queued_requests: int
    api_server_tts_max_new_tokens: int | None
    api_server_decoder_checkpoint_path: Path
    api_server_decoder_config_name: str
    api_server_compile: bool
    api_server_half: bool
    api_server_workers: int
    api_server_max_text_length: int
    api_server_references_dir: Path
    heartbeat_interval_seconds: float
    manage_api_server: bool

    @property
    def api_server_url(self) -> str:
        if self.api_server_url_override:
            return self.api_server_url_override.rstrip("/")
        host = "127.0.0.1" if self.api_server_host in {"0.0.0.0", "::"} else self.api_server_host
        return f"http://{host}:{self.api_server_port}"

    @classmethod
    def from_env(cls) -> "Config":
        manager_url = os.environ["MANAGER_URL"]
        worker_token = os.environ["WORKER_TOKEN"]
        worker_id = os.getenv("WORKER_ID") or f"worker-{uuid.uuid4().hex[:12]}"
        return cls(
            manager_url=manager_url,
            worker_token=worker_token,
            worker_id=worker_id,
            model_id=os.getenv("MODEL_ID", "fishaudio/s2-pro"),
            model_dir=Path(os.getenv("MODEL_DIR", "/models/s2-pro")),
            cache_dir=Path(os.getenv("CACHE_DIR", "/cache")),
            api_server_host=os.getenv("API_SERVER_HOST", "127.0.0.1"),
            api_server_port=env_int("API_SERVER_PORT", 8000),
            api_server_url_override=os.getenv("API_SERVER_URL") or None,
            api_server_max_running_requests=max(1, env_int("API_SERVER_MAX_RUNNING_REQUESTS", 1)),
            api_server_max_queued_requests=max(0, env_int("API_SERVER_MAX_QUEUED_REQUESTS", 1)),
            api_server_tts_max_new_tokens=env_optional_int("API_SERVER_TTS_MAX_NEW_TOKENS"),
            api_server_decoder_checkpoint_path=Path(
                os.getenv("API_SERVER_DECODER_CHECKPOINT_PATH")
                or os.getenv("DECODER_CHECKPOINT_PATH", "/models/s2-pro/codec.pth")
            ),
            api_server_decoder_config_name=os.getenv("API_SERVER_DECODER_CONFIG_NAME", "modded_dac_vq"),
            api_server_compile=env_bool("API_SERVER_COMPILE", True),
            api_server_half=env_bool("API_SERVER_HALF", False),
            api_server_workers=max(1, env_int("API_SERVER_WORKERS", 1)),
            api_server_max_text_length=max(0, env_int("API_SERVER_MAX_TEXT_LENGTH", 0)),
            api_server_references_dir=Path(os.getenv("API_SERVER_REFERENCES_DIR", "references")),
            heartbeat_interval_seconds=float(os.getenv("HEARTBEAT_INTERVAL_SECONDS", "5")),
            manage_api_server=env_bool("WORKER_MANAGE_API_SERVER", True),
        )


class Worker:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.local_inflight = 0
        self.local_queued = 0
        self.api_server_reject_count = 0
        self.api_reference_ids: set[str] = set()
        self.api_reference_locks: dict[str, asyncio.Lock] = {}
        self.capacity_condition = asyncio.Condition()
        self.ewma_latency_ms: float | None = None
        self.last_error: str | None = None
        self.last_api_server_healthy: bool | None = None
        self.last_api_server_health_detail: str | None = None
        self.started_at = datetime.now(timezone.utc)
        self.stop_event = asyncio.Event()
        self.api_server_process: asyncio.subprocess.Process | None = None

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
                "--workers",
                str(self.config.api_server_workers),
                "--max-text-length",
                str(self.config.api_server_max_text_length),
            ]
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
            healthy, detail = await self.api_server_health()
            if healthy:
                log("Fish API server is healthy", api_server_url=self.config.api_server_url)
                return
            self.last_api_server_health_detail = detail
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
            if message.get("type") == "inference_request":
                data = message["data"]
                asyncio.create_task(self.handle_inference(ws, data))
            elif message.get("type") == "cancel_request":
                log("cancel requested", data=message.get("data"))

    def worker_hello(self) -> dict[str, Any]:
        gpu = detect_gpu()
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
            "sglang_url": self.config.api_server_url,
            "started_at": self.started_at.isoformat().replace("+00:00", "Z"),
        }

    async def heartbeat_loop(self, ws: Any) -> None:
        while True:
            healthy, detail = await self.api_server_health()
            self.log_api_server_health_change(healthy, detail)
            gpu = detect_gpu()
            await send_msg(
                ws,
                "heartbeat",
                {
                    "worker_id": self.config.worker_id,
                    "ready": healthy,
                    "sglang_healthy": healthy,
                    "inflight": self.local_inflight,
                    "queued": self.local_queued,
                    "max_running_requests": self.config.api_server_max_running_requests,
                    "max_queued_requests": self.config.api_server_max_queued_requests,
                    "vram_used_mb": gpu.get("vram_used_mb"),
                    "vram_free_mb": gpu.get("vram_free_mb"),
                    "gpu_utilization_percent": gpu.get("gpu_utilization_percent"),
                    "ewma_latency_ms": self.ewma_latency_ms,
                    "last_error": self.last_error,
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
        healthy, _ = await self.api_server_health()
        return healthy

    async def api_server_health(self) -> tuple[bool, str | None]:
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(f"{self.config.api_server_url}/v1/health", timeout=3) as response:
                    if response.status == 200:
                        return True, None
                    return False, f"HTTP {response.status}"
            except Exception as exc:  # noqa: BLE001
                return False, f"{type(exc).__name__}: {exc}"

    async def acquire_request_slot(self, request_id: str) -> bool:
        async with self.capacity_condition:
            if self.local_inflight >= self.config.api_server_max_running_requests:
                if self.local_queued >= self.config.api_server_max_queued_requests:
                    self.api_server_reject_count += 1
                    log(
                        "worker rejected inference",
                        request_id=request_id,
                        reason="overloaded",
                        inflight=self.local_inflight,
                        queued=self.local_queued,
                        max_running=self.config.api_server_max_running_requests,
                        max_queued=self.config.api_server_max_queued_requests,
                    )
                    return False
                self.local_queued += 1
                log(
                    "inference queued",
                    request_id=request_id,
                    inflight=self.local_inflight,
                    queued=self.local_queued,
                )
                try:
                    await self.capacity_condition.wait_for(
                        lambda: self.local_inflight < self.config.api_server_max_running_requests
                        or self.stop_event.is_set()
                    )
                finally:
                    self.local_queued -= 1
                if self.stop_event.is_set():
                    return False

            self.local_inflight += 1
            return True

    async def release_request_slot(self) -> None:
        async with self.capacity_condition:
            self.local_inflight = max(0, self.local_inflight - 1)
            self.capacity_condition.notify(1)

    async def handle_inference(self, ws: Any, request: dict[str, Any]) -> None:
        request_id = request["request_id"]
        api_kind = request.get("api_kind") or "unknown"
        started = time.monotonic()
        slot_acquired = await self.acquire_request_slot(request_id)
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
                        api_reference_id, _ = await self.ensure_api_reference(ref, content_type)
                        continue

                    _, path = await self.ensure_api_reference(ref, content_type)
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

            result = await self.call_api_server(ws, request_id, payload, stream=payload["streaming"])
            if result is not None:
                total_ms = (time.monotonic() - started) * 1000
                timings = dict(result["timings"])
                timings["total_ms"] = total_ms
                timings["reference_ms"] = reference_ms
                log(
                    "inference completed",
                    request_id=request_id,
                    api_kind=api_kind,
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
                )
                await send_msg(
                    ws,
                    "inference_done",
                    {
                        "request_id": request_id,
                        "timings": timings,
                        "audio_bytes": result["audio_bytes"],
                        "chunks": result["chunks"],
                    },
                )
        except aiohttp.ClientError as exc:
            self.last_error = str(exc)
            log(
                "api server request failed",
                request_id=request_id,
                api_kind=api_kind,
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
                error=str(exc),
                total_ms=(time.monotonic() - started) * 1000,
            )
            await send_error(ws, request_id, "inference_failed", str(exc), retryable=False)
        finally:
            elapsed_ms = (time.monotonic() - started) * 1000
            self.update_ewma(elapsed_ms)
            await self.release_request_slot()

    async def download_voice_audio(self, voice_id: str, checksum: str) -> bytes:
        base_url = manager_http_base_url(self.config.manager_url)
        query = urlencode({"checksum": checksum})
        url = f"{base_url}/internal/voices/{quote(voice_id, safe='')}/audio?{query}"
        headers = {"Authorization": f"Bearer {self.config.worker_token}"}
        async with aiohttp.ClientSession() as session:
            started = time.monotonic()
            async with session.get(url, headers=headers, timeout=None) as response:
                if response.status >= 400:
                    text = await response.text()
                    log(
                        "voice audio download failed",
                        voice_id=voice_id,
                        checksum=checksum,
                        status=response.status,
                        elapsed_ms=(time.monotonic() - started) * 1000,
                    )
                    raise RuntimeError(f"failed to fetch voice {voice_id}: HTTP {response.status}: {text}")
                audio = await response.read()
                log(
                    "voice audio downloaded",
                    voice_id=voice_id,
                    checksum=checksum,
                    status=response.status,
                    size_bytes=len(audio),
                    elapsed_ms=(time.monotonic() - started) * 1000,
                )
                return audio

    async def ensure_api_reference(self, ref: dict[str, Any], content_type: str) -> tuple[str, Path]:
        voice_id = ref["voice_id"]
        checksum = ref["checksum"]
        reference_id = api_reference_id_for_voice(voice_id, checksum)
        existing_path = api_reference_existing_audio_path(self.config.api_server_references_dir, reference_id)
        if reference_id in self.api_reference_ids and existing_path:
            return reference_id, existing_path

        lock = self.api_reference_locks.setdefault(reference_id, asyncio.Lock())
        async with lock:
            existing_path = api_reference_existing_audio_path(self.config.api_server_references_dir, reference_id)
            if existing_path:
                self.api_reference_ids.add(reference_id)
                return reference_id, existing_path

            started = time.monotonic()
            audio = await self.download_voice_audio(voice_id, checksum)
            audio_path = write_api_reference(
                self.config.api_server_references_dir,
                reference_id,
                audio,
                ref["text"],
                content_type,
            )
            self.api_reference_ids.add(reference_id)
            log(
                "API reference written",
                reference_id=reference_id,
                voice_id=voice_id,
                checksum=checksum,
                size_bytes=len(audio),
                elapsed_ms=(time.monotonic() - started) * 1000,
            )
            return reference_id, audio_path

    async def call_api_server(
        self, ws: Any, request_id: str, payload: dict[str, Any], stream: bool
    ) -> dict[str, Any] | None:
        started = time.monotonic()
        chunk_send_ms = 0.0
        first_chunk_ms: float | None = None
        audio_bytes = 0
        chunks = 0
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.config.api_server_url}/v1/tts",
                data=msgpack.packb(payload, use_bin_type=True),
                headers={"Content-Type": "application/msgpack"},
                timeout=None,
            ) as response:
                if response.status == 429:
                    self.api_server_reject_count += 1
                    text = await response.text()
                    log(
                        "Fish API server rejected inference",
                        request_id=request_id,
                        status=response.status,
                        elapsed_ms=(time.monotonic() - started) * 1000,
                    )
                    await send_error(ws, request_id, "overloaded", text or "Fish API server overloaded", retryable=True)
                    return None
                if response.status >= 400:
                    text = await response.text()
                    code = "bad_request" if response.status < 500 else "sglang_unavailable"
                    log(
                        "Fish API server inference failed",
                        request_id=request_id,
                        status=response.status,
                        code=code,
                        elapsed_ms=(time.monotonic() - started) * 1000,
                    )
                    await send_error(ws, request_id, code, text, retryable=response.status >= 500)
                    return None

                content_type = response.headers.get("content-type", "application/octet-stream")
                if stream:
                    seq = 0
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        if chunk:
                            if first_chunk_ms is None:
                                first_chunk_ms = (time.monotonic() - started) * 1000
                            audio_bytes += len(chunk)
                            chunks += 1
                            send_started = time.monotonic()
                            await send_chunk(ws, request_id, seq, content_type, chunk)
                            chunk_send_ms += (time.monotonic() - send_started) * 1000
                            seq += 1
                else:
                    body = await response.read()
                    first_chunk_ms = (time.monotonic() - started) * 1000
                    audio_bytes = len(body)
                    chunks = 1 if body else 0
                    send_started = time.monotonic()
                    await send_chunk(ws, request_id, 0, content_type, body)
                    chunk_send_ms += (time.monotonic() - send_started) * 1000

                total_api_server_ms = (time.monotonic() - started) * 1000
                upstream_ms = max(0.0, total_api_server_ms - chunk_send_ms)
                log(
                    "Fish API server inference completed",
                    request_id=request_id,
                    stream=stream,
                    status=response.status,
                    content_type=content_type,
                    audio_bytes=audio_bytes,
                    chunks=chunks,
                    api_server_ms=upstream_ms,
                    chunk_send_ms=chunk_send_ms,
                    first_chunk_ms=first_chunk_ms,
                )
                return {
                    "audio_bytes": audio_bytes,
                    "chunks": chunks,
                    "content_type": content_type,
                    "timings": {
                        "total_ms": 0.0,
                        "reference_ms": 0.0,
                        "api_server_ms": upstream_ms,
                        "sglang_ms": upstream_ms,
                        "chunk_send_ms": chunk_send_ms,
                        "first_chunk_ms": first_chunk_ms,
                    },
                }

    def update_ewma(self, elapsed_ms: float) -> None:
        if self.ewma_latency_ms is None:
            self.ewma_latency_ms = elapsed_ms
        else:
            self.ewma_latency_ms = self.ewma_latency_ms * 0.8 + elapsed_ms * 0.2


async def send_msg(ws: Any, msg_type: str, data: dict[str, Any]) -> None:
    await ws.send(msgpack.packb({"type": msg_type, "data": data}, use_bin_type=True))


def unpack_msg(raw: bytes) -> dict[str, Any]:
    return msgpack.unpackb(raw, raw=False)


async def send_chunk(ws: Any, request_id: str, seq: int, content_type: str, chunk: bytes) -> None:
    await send_msg(
        ws,
        "inference_chunk",
        {
            "request_id": request_id,
            "seq": seq,
            "content_type": content_type,
            "bytes": chunk,
            "is_sse": False,
        },
    )


async def send_error(ws: Any, request_id: str, code: str, message: str, retryable: bool) -> None:
    await send_msg(
        ws,
        "inference_error",
        {
            "request_id": request_id,
            "code": code,
            "message": message,
            "retryable": retryable,
        },
    )


def append_token(url: str, token: str) -> str:
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}token={token}"


def redact_query_token(url: str) -> str:
    parts = urlsplit(url)
    query = "&".join(
        "token=<redacted>" if item.startswith("token=") else item for item in parts.query.split("&") if item
    )
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


def manager_http_base_url(manager_url: str) -> str:
    parts = urlsplit(manager_url)
    scheme = {"ws": "http", "wss": "https"}.get(parts.scheme, parts.scheme)
    path = parts.path.rstrip("/")
    suffix = "/internal/workers/ws"
    if path.endswith(suffix):
        path = path[: -len(suffix)]
    return urlunsplit((scheme, parts.netloc, path.rstrip("/"), "", ""))


def suffix_for_content_type(content_type: str) -> str:
    return {
        "audio/mpeg": ".mp3",
        "audio/mp3": ".mp3",
        "audio/flac": ".flac",
        "audio/ogg": ".ogg",
        "audio/wav": ".wav",
        "audio/x-wav": ".wav",
    }.get(content_type, ".wav")


def api_reference_existing_audio_path(references_dir: Path, reference_id: str) -> Path | None:
    ref_dir = references_dir / reference_id
    if not ref_dir.is_dir():
        return None

    for audio_path in ref_dir.iterdir():
        if audio_path.suffix != ".lab" and audio_path.is_file() and audio_path.with_suffix(".lab").is_file():
            return audio_path
    return None


def write_api_reference(
    references_dir: Path,
    reference_id: str,
    audio: bytes,
    text: str,
    content_type: str,
) -> Path:
    references_dir.mkdir(parents=True, exist_ok=True)
    suffix = suffix_for_content_type(content_type)
    ref_dir = references_dir / reference_id
    tmp_dir = references_dir / f".{reference_id}.{uuid.uuid4().hex}.tmp"
    audio_path = ref_dir / f"sample{suffix}"
    try:
        tmp_dir.mkdir(parents=True, exist_ok=False)
        (tmp_dir / f"sample{suffix}").write_bytes(audio)
        (tmp_dir / "sample.lab").write_text(text, encoding="utf-8")
        tmp_dir.replace(ref_dir)
    except FileExistsError:
        existing_path = api_reference_existing_audio_path(references_dir, reference_id)
        if existing_path:
            return existing_path
        raise
    finally:
        with contextlib.suppress(FileNotFoundError):
            shutil.rmtree(tmp_dir)
    return audio_path


def api_server_payload(payload: dict[str, Any], stream: bool) -> dict[str, Any]:
    api_payload = {
        key: value
        for key, value in payload.items()
        if key not in {"input", "response_format", "stream", "voice"}
    }
    api_payload["text"] = payload.get("text") or payload.get("input") or ""
    response_format = payload.get("format") or payload.get("response_format")
    if response_format:
        api_payload["format"] = response_format
    api_payload["streaming"] = bool(payload.get("streaming", stream))
    return api_payload


def api_reference_id_for_voice(voice_id: str, checksum: str) -> str:
    safe_voice_id = re.sub(r"[^a-zA-Z0-9\-_ ]+", "_", voice_id).strip(" _-") or "voice"
    checksum_part = re.sub(r"[^a-zA-Z0-9\-_]+", "", checksum)[:64] or uuid.uuid5(
        uuid.NAMESPACE_URL, checksum
    ).hex
    max_voice_len = max(1, 255 - len(checksum_part) - 1)
    return f"{safe_voice_id[:max_voice_len]}-{checksum_part}"


def detect_gpu() -> dict[str, Any]:
    try:
        output = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,memory.used,memory.free,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            text=True,
            timeout=2,
        )
    except Exception:  # noqa: BLE001
        return {"gpu_count": 0}

    rows = [row.strip() for row in output.splitlines() if row.strip()]
    if not rows:
        return {"gpu_count": 0}

    first = [part.strip() for part in rows[0].split(",")]
    try:
        return {
            "gpu_name": first[0],
            "gpu_count": len(rows),
            "vram_total_mb": int(first[1]),
            "vram_used_mb": int(first[2]),
            "vram_free_mb": int(first[3]),
            "gpu_utilization_percent": float(first[4]),
        }
    except (IndexError, ValueError):
        return {"gpu_count": len(rows), "gpu_name": first[0] if first else None}


def install_signal_handlers(worker: Worker) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, worker.stop_event.set)


async def async_main() -> None:
    config = Config.from_env()
    worker = Worker(config)
    install_signal_handlers(worker)
    try:
        await worker.run()
    finally:
        if worker.api_server_process and worker.api_server_process.returncode is None:
            worker.api_server_process.terminate()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(worker.api_server_process.wait(), timeout=20)


def run() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    run()
