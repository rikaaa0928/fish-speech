from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import shlex
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp
import msgpack
import websockets

from fish_worker import __version__


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


@dataclass(frozen=True)
class Config:
    manager_url: str
    worker_token: str
    worker_id: str
    model_id: str
    model_dir: Path
    cache_dir: Path
    sglang_host: str
    sglang_port: int
    sglang_config: str
    sglang_max_running_requests: int
    sglang_max_queued_requests: int
    sglang_tts_max_new_tokens: int | None
    worker_max_inflight: int
    worker_max_queue: int
    heartbeat_interval_seconds: float
    manage_sglang: bool

    @property
    def sglang_url(self) -> str:
        return f"http://{self.sglang_host}:{self.sglang_port}"

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
            sglang_host=os.getenv("SGLANG_HOST", "127.0.0.1"),
            sglang_port=env_int("SGLANG_PORT", 8000),
            sglang_config=os.getenv("SGLANG_CONFIG", "/app/configs/s2pro_tts.yaml"),
            sglang_max_running_requests=env_int("SGLANG_MAX_RUNNING_REQUESTS", 4),
            sglang_max_queued_requests=env_int("SGLANG_MAX_QUEUED_REQUESTS", 2),
            sglang_tts_max_new_tokens=env_optional_int("SGLANG_TTS_MAX_NEW_TOKENS"),
            worker_max_inflight=env_int("WORKER_MAX_INFLIGHT", 4),
            worker_max_queue=env_int("WORKER_MAX_QUEUE", 2),
            heartbeat_interval_seconds=float(os.getenv("HEARTBEAT_INTERVAL_SECONDS", "5")),
            manage_sglang=env_bool("WORKER_MANAGE_SGLANG", True),
        )


class Worker:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.local_inflight = 0
        self.local_queue = 0
        self.local_reject_count = 0
        self.sglang_reject_count = 0
        self.ewma_latency_ms: float | None = None
        self.last_error: str | None = None
        self.started_at = datetime.now(timezone.utc)
        self.stop_event = asyncio.Event()
        self.sglang_process: asyncio.subprocess.Process | None = None

    async def run(self) -> None:
        self.config.cache_dir.mkdir(parents=True, exist_ok=True)
        (self.config.cache_dir / "refs").mkdir(parents=True, exist_ok=True)

        if self.config.manage_sglang:
            self.sglang_process = await self.start_sglang()

        await self.wait_sglang_ready()

        while not self.stop_event.is_set():
            try:
                await self.connect_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)
                print(f"worker connection failed: {exc}", flush=True)
                await asyncio.sleep(5)

    async def start_sglang(self) -> asyncio.subprocess.Process:
        cmd = os.getenv("SGLANG_COMMAND")
        if cmd:
            args = ["bash", "-lc", cmd]
        else:
            args = [
                "sgl-omni",
                "serve",
                "--model-path",
                str(self.config.model_dir),
                "--host",
                self.config.sglang_host,
                "--port",
                str(self.config.sglang_port),
            ]
            if self.config.sglang_config:
                args[4:4] = ["--config", self.config.sglang_config]
            else:
                args.extend(
                    [
                        "--max-running-requests",
                        str(self.config.sglang_max_running_requests),
                        "--max-queued-requests",
                        str(self.config.sglang_max_queued_requests),
                    ]
                )
            extra_args = os.getenv("SGLANG_EXTRA_ARGS")
            if extra_args:
                args.extend(shlex.split(extra_args))

        print(f"starting SGLang: {' '.join(args)}", flush=True)
        return await asyncio.create_subprocess_exec(*args)

    async def wait_sglang_ready(self) -> None:
        deadline = time.monotonic() + env_int("SGLANG_STARTUP_TIMEOUT_SECONDS", 900)
        while time.monotonic() < deadline:
            if await self.sglang_healthy():
                print("SGLang is healthy", flush=True)
                return
            await asyncio.sleep(2)
        raise RuntimeError("SGLang did not become healthy before timeout")

    async def connect_once(self) -> None:
        url = append_token(self.config.manager_url, self.config.worker_token)
        async with websockets.connect(url, max_size=None, ping_interval=20, ping_timeout=20) as ws:
            print(f"connected to manager as {self.config.worker_id}", flush=True)
            await send_msg(ws, "worker_hello", self.worker_hello())

            heartbeat_task = asyncio.create_task(self.heartbeat_loop(ws))
            try:
                async for raw in ws:
                    if isinstance(raw, str):
                        continue
                    message = unpack_msg(raw)
                    if message.get("type") == "inference_request":
                        data = message["data"]
                        if self.is_overloaded():
                            self.local_reject_count += 1
                            await send_error(
                                ws,
                                data["request_id"],
                                "overloaded",
                                "Worker is overloaded",
                                retryable=True,
                            )
                            continue
                        asyncio.create_task(self.handle_inference(ws, data))
                    elif message.get("type") == "cancel_request":
                        print(f"cancel requested: {message.get('data')}", flush=True)
            finally:
                heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat_task

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
            "max_running_requests": self.config.sglang_max_running_requests,
            "max_queued_requests": self.config.sglang_max_queued_requests,
            "worker_max_inflight": self.config.worker_max_inflight,
            "worker_max_queue": self.config.worker_max_queue,
            "sglang_url": self.config.sglang_url,
            "started_at": self.started_at.isoformat().replace("+00:00", "Z"),
        }

    async def heartbeat_loop(self, ws: Any) -> None:
        while True:
            healthy = await self.sglang_healthy()
            gpu = detect_gpu()
            await send_msg(
                ws,
                "heartbeat",
                {
                    "worker_id": self.config.worker_id,
                    "ready": healthy and not self.is_overloaded(),
                    "sglang_healthy": healthy,
                    "inflight": self.local_inflight,
                    "queued": self.local_queue,
                    "max_running_requests": self.config.sglang_max_running_requests,
                    "max_queued_requests": self.config.sglang_max_queued_requests,
                    "vram_used_mb": gpu.get("vram_used_mb"),
                    "vram_free_mb": gpu.get("vram_free_mb"),
                    "gpu_utilization_percent": gpu.get("gpu_utilization_percent"),
                    "ewma_latency_ms": self.ewma_latency_ms,
                    "last_error": self.last_error,
                },
            )
            await asyncio.sleep(self.config.heartbeat_interval_seconds)

    def is_overloaded(self) -> bool:
        capacity = self.config.worker_max_inflight + self.config.worker_max_queue
        return capacity <= 0 or self.local_inflight + self.local_queue >= capacity

    async def sglang_healthy(self) -> bool:
        async with aiohttp.ClientSession() as session:
            for path in ("/health", "/v1/models"):
                try:
                    async with session.get(f"{self.config.sglang_url}{path}", timeout=3) as response:
                        if 200 <= response.status < 500:
                            return True
                except Exception:  # noqa: BLE001
                    continue
        return False

    async def handle_inference(self, ws: Any, request: dict[str, Any]) -> None:
        request_id = request["request_id"]
        self.local_inflight += 1
        started = time.monotonic()
        temp_files: list[Path] = []

        try:
            payload = dict(request.get("payload") or {})
            if self.config.sglang_tts_max_new_tokens is not None:
                payload.setdefault("max_new_tokens", self.config.sglang_tts_max_new_tokens)
            refs = []
            for index, ref in enumerate(request.get("references") or []):
                content_type = ref.get("content_type") or "audio/wav"
                suffix = suffix_for_content_type(content_type)
                path = self.config.cache_dir / "refs" / f"{request_id}_{index}{suffix}"
                path.write_bytes(ref["audio_bytes"])
                temp_files.append(path)
                refs.append({"audio_path": str(path), "text": ref["text"]})

            if refs:
                payload["references"] = refs
            payload["stream"] = bool(request.get("stream"))

            completed = await self.call_sglang(ws, request_id, payload, stream=payload["stream"])
            if completed:
                await send_msg(ws, "inference_done", {"request_id": request_id})
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            await send_error(ws, request_id, "inference_failed", str(exc), retryable=False)
        finally:
            elapsed_ms = (time.monotonic() - started) * 1000
            self.update_ewma(elapsed_ms)
            self.local_inflight -= 1
            for path in temp_files:
                with contextlib.suppress(FileNotFoundError):
                    path.unlink()

    async def call_sglang(self, ws: Any, request_id: str, payload: dict[str, Any], stream: bool) -> bool:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.config.sglang_url}/v1/audio/speech",
                json=payload,
                timeout=None,
            ) as response:
                if response.status == 429:
                    self.sglang_reject_count += 1
                    text = await response.text()
                    await send_error(ws, request_id, "overloaded", text or "SGLang overloaded", retryable=True)
                    return False
                if response.status >= 400:
                    text = await response.text()
                    code = "bad_request" if response.status < 500 else "sglang_unavailable"
                    await send_error(ws, request_id, code, text, retryable=response.status >= 500)
                    return False

                content_type = response.headers.get("content-type", "application/octet-stream")
                if stream:
                    seq = 0
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        if chunk:
                            await send_chunk(ws, request_id, seq, content_type, chunk)
                            seq += 1
                else:
                    body = await response.read()
                    await send_chunk(ws, request_id, 0, content_type, body)

                return True

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


def suffix_for_content_type(content_type: str) -> str:
    return {
        "audio/mpeg": ".mp3",
        "audio/mp3": ".mp3",
        "audio/flac": ".flac",
        "audio/ogg": ".ogg",
        "audio/wav": ".wav",
        "audio/x-wav": ".wav",
    }.get(content_type, ".wav")


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
        if worker.sglang_process and worker.sglang_process.returncode is None:
            worker.sglang_process.terminate()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(worker.sglang_process.wait(), timeout=20)


def run() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    run()
