from __future__ import annotations

import contextlib
import re
import shutil
import uuid
from pathlib import Path


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


def api_reference_id_for_voice(voice_id: str, checksum: str) -> str:
    safe_voice_id = re.sub(r"[^a-zA-Z0-9\-_ ]+", "_", voice_id).strip(" _-") or "voice"
    checksum_part = re.sub(r"[^a-zA-Z0-9\-_]+", "", checksum)[:64] or uuid.uuid5(
        uuid.NAMESPACE_URL, checksum
    ).hex
    max_voice_len = max(1, 255 - len(checksum_part) - 1)
    return f"{safe_voice_id[:max_voice_len]}-{checksum_part}"
