from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlencode

import aiohttp

from fish_worker.logging_utils import log
from fish_worker.protocol import manager_http_base_url
from fish_worker.references import (
    api_reference_existing_audio_path,
    api_reference_id_for_voice,
    write_api_reference,
)

if TYPE_CHECKING:
    from fish_worker.worker import Worker


async def download_voice_audio(worker: "Worker", voice_id: str, checksum: str) -> bytes:
    base_url = manager_http_base_url(worker.config.manager_url)
    query = urlencode({"checksum": checksum})
    url = f"{base_url}/internal/voices/{quote(voice_id, safe='')}/audio?{query}"
    headers = {"Authorization": f"Bearer {worker.config.worker_token}"}
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


async def ensure_api_reference(worker: "Worker", ref: dict[str, Any], content_type: str) -> tuple[str, Path]:
    voice_id = ref["voice_id"]
    checksum = ref["checksum"]
    reference_id = api_reference_id_for_voice(voice_id, checksum)
    existing_path = api_reference_existing_audio_path(worker.config.api_server_references_dir, reference_id)
    if reference_id in worker.api_reference_ids and existing_path:
        return reference_id, existing_path

    lock = worker.api_reference_locks.setdefault(reference_id, asyncio.Lock())
    async with lock:
        existing_path = api_reference_existing_audio_path(worker.config.api_server_references_dir, reference_id)
        if existing_path:
            worker.api_reference_ids.add(reference_id)
            return reference_id, existing_path

        started = time.monotonic()
        audio = await download_voice_audio(worker, voice_id, checksum)
        audio_path = write_api_reference(
            worker.config.api_server_references_dir,
            reference_id,
            audio,
            ref["text"],
            content_type,
        )
        worker.api_reference_ids.add(reference_id)
        log(
            "API reference written",
            reference_id=reference_id,
            voice_id=voice_id,
            checksum=checksum,
            size_bytes=len(audio),
            elapsed_ms=(time.monotonic() - started) * 1000,
        )
        return reference_id, audio_path
