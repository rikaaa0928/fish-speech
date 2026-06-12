#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "httpx>=0.27.0",
# ]
# ///

from __future__ import annotations

import argparse
import mimetypes
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin

import httpx

DEFAULT_BASE_URL = "http://127.0.0.1:8080"
DEFAULT_SOURCE_DIR = "/home/hypatia/src/fish-speech/references"
AUDIO_EXTENSIONS = {
    ".mp3",
    ".wav",
    ".flac",
    ".ogg",
    ".m4a",
    ".wma",
    ".aac",
    ".aiff",
    ".aif",
    ".aifc",
}
REFERENCE_ID_PATTERN = re.compile(r"^[a-zA-Z0-9\-_ ]{1,255}$")


@dataclass(frozen=True)
class ReferencePair:
    reference_id: str
    upload_id: str
    audio_path: Path
    text_path: Path
    text: str


def first_api_key() -> str | None:
    if key := os.getenv("OPENAI_API_KEY"):
        return key
    keys = os.getenv("OPENAI_API_KEYS", "")
    return next((key.strip() for key in keys.split(",") if key.strip()), None)


def normalize_base_url(base_url: str) -> str:
    return base_url.rstrip("/") + "/"


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def find_audio_text_pairs(
    ref_dir: Path,
    *,
    multi_policy: str,
) -> tuple[list[ReferencePair], list[str]]:
    warnings: list[str] = []
    reference_id = ref_dir.name

    if not REFERENCE_ID_PATTERN.fullmatch(reference_id):
        warnings.append(f"skip {reference_id!r}: invalid reference id")
        return [], warnings

    candidates: list[tuple[Path, Path]] = []
    for audio_path in sorted(ref_dir.rglob("*"), key=lambda p: p.relative_to(ref_dir).as_posix()):
        if not audio_path.is_file() or audio_path.suffix.lower() not in AUDIO_EXTENSIONS:
            continue
        text_path = audio_path.with_suffix(".lab")
        if text_path.exists():
            candidates.append((audio_path, text_path))

    if not candidates:
        warnings.append(f"skip {reference_id!r}: no audio file with matching .lab")
        return [], warnings

    if len(candidates) > 1:
        warnings.append(
            f"{reference_id!r}: found {len(candidates)} audio/.lab pairs; policy={multi_policy}"
        )
        if multi_policy == "skip":
            return [], warnings
        if multi_policy == "first":
            candidates = candidates[:1]

    pairs: list[ReferencePair] = []
    for index, (audio_path, text_path) in enumerate(candidates, start=1):
        text = read_text(text_path)
        if not text:
            warnings.append(f"skip {reference_id!r}: empty text file {text_path}")
            continue
        upload_id = reference_id if index == 1 else f"{reference_id}__{index}"
        if not REFERENCE_ID_PATTERN.fullmatch(upload_id):
            warnings.append(f"skip {upload_id!r}: generated reference id is invalid")
            continue
        pairs.append(
            ReferencePair(
                reference_id=reference_id,
                upload_id=upload_id,
                audio_path=audio_path,
                text_path=text_path,
                text=text,
            )
        )

    return pairs, warnings


def discover_references(source_dir: Path, *, multi_policy: str) -> tuple[list[ReferencePair], list[str]]:
    if not source_dir.exists():
        raise FileNotFoundError(f"source directory does not exist: {source_dir}")
    if not source_dir.is_dir():
        raise NotADirectoryError(f"source path is not a directory: {source_dir}")

    pairs: list[ReferencePair] = []
    warnings: list[str] = []
    for ref_dir in sorted((path for path in source_dir.iterdir() if path.is_dir()), key=lambda p: p.name):
        found, found_warnings = find_audio_text_pairs(ref_dir, multi_policy=multi_policy)
        pairs.extend(found)
        warnings.extend(found_warnings)
    return pairs, warnings


def list_existing(client: httpx.Client, base_url: str) -> set[str]:
    response = client.get(urljoin(base_url, "v1/references/list"))
    response.raise_for_status()
    body = response.json()
    return set(body.get("reference_ids") or [])


def delete_reference(client: httpx.Client, base_url: str, reference_id: str) -> None:
    response = client.request(
        "DELETE",
        urljoin(base_url, "v1/references/delete"),
        json={"reference_id": reference_id},
    )
    response.raise_for_status()


def upload_reference(client: httpx.Client, base_url: str, pair: ReferencePair) -> None:
    content_type = mimetypes.guess_type(pair.audio_path.name)[0] or "audio/wav"
    with pair.audio_path.open("rb") as audio_file:
        response = client.post(
            urljoin(base_url, "v1/references/add"),
            data={"id": pair.upload_id, "text": pair.text},
            files={"audio": (pair.audio_path.name, audio_file, content_type)},
        )
    response.raise_for_status()
    body = response.json()
    if body.get("success") is not True:
        raise RuntimeError(f"add reference returned unsuccessful response: {body}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Migrate old Fish reference voices into fish-manager /v1/references/add."
    )
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=Path(os.getenv("OLD_REFERENCES_DIR", DEFAULT_SOURCE_DIR)),
        help=f"Old references directory, default {DEFAULT_SOURCE_DIR}",
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("FISH_MANAGER_BASE_URL", DEFAULT_BASE_URL),
        help="fish-manager HTTP base URL, defaults to FISH_MANAGER_BASE_URL or localhost",
    )
    parser.add_argument(
        "--api-key",
        default=first_api_key(),
        help="OpenAI-compatible API key, defaults to OPENAI_API_KEY or first OPENAI_API_KEYS",
    )
    parser.add_argument(
        "--multi-policy",
        choices=("first", "skip", "all"),
        default="first",
        help="How to handle old reference IDs with multiple audio/.lab pairs",
    )
    parser.add_argument(
        "--replace-existing",
        action="store_true",
        help="Delete existing references with the same ID before uploading",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be uploaded without calling the API",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="HTTP timeout in seconds",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.api_key and not args.dry_run:
        print("error: --api-key is required unless OPENAI_API_KEY or OPENAI_API_KEYS is set", file=sys.stderr)
        return 2

    base_url = normalize_base_url(args.base_url)
    pairs, warnings = discover_references(args.source_dir, multi_policy=args.multi_policy)

    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)

    if not pairs:
        print("no references to migrate")
        return 0

    print(f"found {len(pairs)} reference upload(s) under {args.source_dir}")
    if args.dry_run:
        for pair in pairs:
            rel_audio = pair.audio_path.relative_to(args.source_dir)
            rel_text = pair.text_path.relative_to(args.source_dir)
            print(f"dry-run: {pair.upload_id!r} audio={rel_audio} text={rel_text}")
        return 0

    headers = {"Authorization": f"Bearer {args.api_key}"}
    uploaded = 0
    skipped = 0
    failed = 0
    with httpx.Client(headers=headers, timeout=args.timeout) as client:
        existing = list_existing(client, base_url)
        for pair in pairs:
            if pair.upload_id in existing:
                if not args.replace_existing:
                    print(f"skip existing: {pair.upload_id}")
                    skipped += 1
                    continue
                print(f"replace existing: {pair.upload_id}")
                delete_reference(client, base_url, pair.upload_id)
                existing.discard(pair.upload_id)

            try:
                upload_reference(client, base_url, pair)
            except Exception as exc:  # noqa: BLE001
                print(f"failed: {pair.upload_id}: {exc}", file=sys.stderr)
                failed += 1
                continue

            existing.add(pair.upload_id)
            uploaded += 1
            print(f"uploaded: {pair.upload_id}")

    print(f"done: uploaded={uploaded} skipped={skipped} failed={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
