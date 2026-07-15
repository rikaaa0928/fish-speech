from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import aiohttp
import msgpack

from fish_worker.logging_utils import log
from fish_worker.protocol import send_chunk, send_error

if TYPE_CHECKING:
    from fish_worker.worker import Worker


async def call_api_server(
    worker: "Worker",
    ws: Any,
    request_id: str,
    payload: dict[str, Any],
    stream: bool,
) -> dict[str, Any] | None:
    started = time.monotonic()
    chunk_send_ms = 0.0
    first_chunk_ms: float | None = None
    audio_bytes = 0
    chunks = 0
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{worker.config.api_server_url}/v1/tts",
            data=msgpack.packb(payload, use_bin_type=True),
            headers={"Content-Type": "application/msgpack"},
            timeout=None,
        ) as response:
            if response.status == 429:
                worker.api_server_reject_count += 1
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
                if response.status >= 500:
                    await worker.record_api_server_failure(f"inference HTTP {response.status}: {text[:200]}")
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
            worker.api_server_watchdog_failures = 0
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
