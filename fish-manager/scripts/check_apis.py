#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "httpx>=0.27.0",
#   "websockets>=12.0",
# ]
# ///

from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import os
import sys
import time
import uuid
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urljoin, urlparse, urlunparse

import httpx
import websockets

DEFAULT_BASE_URL = "http://127.0.0.1:8080"
CHECK_ORDER = [
    "health",
    "workers",
    "references-list",
    "references-add",
    "references-update",
    "audio-speech",
    "tts",
    "references-delete",
    "worker-ws",
]
REFERENCE_CHECKS = {"references-add", "references-update", "references-delete"}
SPEECH_CHECKS = {"audio-speech", "tts"}
PUBLIC_CHECKS = set(CHECK_ORDER) - {"worker-ws"}
AUTH_CHECKS = PUBLIC_CHECKS - {"health"}
CHECK_ALIASES = {
    "all": set(CHECK_ORDER),
    "public": PUBLIC_CHECKS,
    "references": {
        "references-list",
        "references-add",
        "references-update",
        "references-delete",
    },
    "voices": {
        "references-list",
        "references-add",
        "references-update",
        "references-delete",
    },
    "speech": SPEECH_CHECKS,
    "health": {"health"},
    "/health": {"health"},
    "get /health": {"health"},
    "workers": {"workers"},
    "/v1/workers": {"workers"},
    "get /v1/workers": {"workers"},
    "references-list": {"references-list"},
    "list-references": {"references-list"},
    "get /v1/references/list": {"references-list"},
    "references-add": {"references-add"},
    "add-reference": {"references-add"},
    "post /v1/references/add": {"references-add"},
    "references-update": {"references-update"},
    "update-reference": {"references-update"},
    "post /v1/references/update": {"references-update"},
    "references-delete": {"references-delete"},
    "delete-reference": {"references-delete"},
    "delete /v1/references/delete": {"references-delete"},
    "voices-list": {"references-list"},
    "list-voices": {"references-list"},
    "voices-create": {"references-add"},
    "create-voice": {"references-add"},
    "voices-delete": {"references-delete"},
    "delete-voice": {"references-delete"},
    "audio-speech": {"audio-speech"},
    "speech-create": {"audio-speech"},
    "post /v1/audio/speech": {"audio-speech"},
    "tts": {"tts"},
    "fish-tts": {"tts"},
    "post /v1/tts": {"tts"},
    "worker-ws": {"worker-ws"},
    "internal-ws": {"worker-ws"},
    "get /internal/workers/ws": {"worker-ws"},
}


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str
    warning: bool = False
    response_body: str | None = None


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def first_api_key() -> str | None:
    if key := os.getenv("OPENAI_API_KEY"):
        return key
    keys = os.getenv("OPENAI_API_KEYS", "")
    return next((key.strip() for key in keys.split(",") if key.strip()), None)


def tiny_wav_base64() -> str:
    sample_rate = 16_000
    duration_seconds = 0.05
    sample_count = int(sample_rate * duration_seconds)
    pcm = b"\x00\x00" * sample_count
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def ws_url_from_base(base_url: str, token: str) -> str:
    parsed = urlparse(base_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    path = urljoin(parsed.path.rstrip("/") + "/", "internal/workers/ws")
    query = urlencode({"token": token})
    return urlunparse((scheme, parsed.netloc, path, "", query, ""))


def json_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:200]
    return str(payload)[:300]


def http_error_detail(error: httpx.HTTPError) -> str:
    detail = str(error).strip()
    if detail:
        return f"{error.__class__.__name__}: {detail}"
    return error.__class__.__name__


def response_body(response: httpx.Response) -> str | None:
    if not response.content:
        return None

    try:
        payload = response.json()
    except ValueError:
        text = response.text.strip()
        return text or None
    return json.dumps(payload, ensure_ascii=False, indent=2)


def is_binary_response(response: httpx.Response) -> bool:
    content_type = (
        response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    )
    return (
        content_type.startswith("audio/") or content_type == "application/octet-stream"
    )


def audio_extension(response: httpx.Response) -> str:
    content_type = (
        response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    )
    return {
        "audio/wav": "wav",
        "audio/wave": "wav",
        "audio/x-wav": "wav",
        "audio/mpeg": "mp3",
        "audio/mp3": "mp3",
        "audio/ogg": "ogg",
        "audio/flac": "flac",
    }.get(content_type, "bin")


def slugify(value: str) -> str:
    slug = "".join(char.lower() if char.isalnum() else "-" for char in value)
    return "-".join(part for part in slug.split("-") if part) or "audio"


def save_audio_response(
    name: str, response: httpx.Response, audio_dir: Path | None
) -> Path | None:
    if audio_dir is None or not is_binary_response(response):
        return None

    audio_dir.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    filename = f"{timestamp}-{slugify(name)}-{uuid.uuid4().hex[:8]}.{audio_extension(response)}"
    path = audio_dir / filename
    path.write_bytes(response.content)
    return path


def result_from_response(
    name: str,
    response: httpx.Response,
    expected_status: int,
    validate: Any | None = None,
) -> CheckResult:
    if response.status_code != expected_status:
        return CheckResult(
            name,
            False,
            f"expected {expected_status}, got {response.status_code}: {json_detail(response)}",
            response_body=response_body(response),
        )
    if validate is not None:
        error = validate(response)
        if error:
            return CheckResult(
                name, False, error, response_body=response_body(response)
            )
    return CheckResult(
        name,
        True,
        f"HTTP {response.status_code}",
        response_body=response_body(response),
    )


async def check_health(client: httpx.AsyncClient) -> CheckResult:
    try:
        response = await client.get("/health")
    except httpx.HTTPError as error:
        return CheckResult("GET /health", False, http_error_detail(error))

    def validate(response: httpx.Response) -> str | None:
        if response.json().get("status") != "ok":
            return f"unexpected body: {json_detail(response)}"
        return None

    return result_from_response("GET /health", response, 200, validate)


async def check_workers(client: httpx.AsyncClient) -> CheckResult:
    try:
        response = await client.get("/v1/workers")
    except httpx.HTTPError as error:
        return CheckResult("GET /v1/workers", False, http_error_detail(error))

    def validate(response: httpx.Response) -> str | None:
        data = response.json().get("data")
        if not isinstance(data, list):
            return f"expected data list, got: {json_detail(response)}"
        return None

    return result_from_response("GET /v1/workers", response, 200, validate)


async def check_list_references(client: httpx.AsyncClient) -> CheckResult:
    try:
        response = await client.get("/v1/references/list")
    except httpx.HTTPError as error:
        return CheckResult("GET /v1/references/list", False, http_error_detail(error))

    def validate(response: httpx.Response) -> str | None:
        payload = response.json()
        if payload.get("success") is not True or not isinstance(
            payload.get("reference_ids"), list
        ):
            return f"expected reference_ids list, got: {json_detail(response)}"
        return None

    return result_from_response("GET /v1/references/list", response, 200, validate)


async def add_reference(
    client: httpx.AsyncClient, reference_id: str
) -> tuple[CheckResult, bool]:
    files = {
        "id": (None, reference_id),
        "text": (None, "fish-manager api availability test reference"),
        "audio": ("reference.wav", base64.b64decode(tiny_wav_base64()), "audio/wav"),
    }
    try:
        response = await client.post("/v1/references/add", files=files)
    except httpx.HTTPError as error:
        return CheckResult("POST /v1/references/add", False, http_error_detail(error)), False

    def validate(response: httpx.Response) -> str | None:
        payload = response.json()
        if payload.get("success") is not True or payload.get("reference_id") != reference_id:
            return f"unexpected reference_id: {json_detail(response)}"
        return None

    result = result_from_response("POST /v1/references/add", response, 200, validate)
    return result, result.ok


async def check_update_reference(
    client: httpx.AsyncClient, old_reference_id: str, new_reference_id: str
) -> CheckResult:
    payload = {
        "old_reference_id": old_reference_id,
        "new_reference_id": new_reference_id,
    }
    try:
        response = await client.post("/v1/references/update", json=payload)
    except httpx.HTTPError as error:
        return CheckResult("POST /v1/references/update", False, http_error_detail(error))

    def validate(response: httpx.Response) -> str | None:
        payload = response.json()
        if (
            payload.get("success") is not True
            or payload.get("old_reference_id") != old_reference_id
            or payload.get("new_reference_id") != new_reference_id
        ):
            return f"unexpected body: {json_detail(response)}"
        return None

    return result_from_response("POST /v1/references/update", response, 200, validate)


async def check_delete_reference(
    client: httpx.AsyncClient, reference_id: str
) -> CheckResult:
    try:
        response = await client.request(
            "DELETE",
            "/v1/references/delete",
            json={"reference_id": reference_id},
        )
    except httpx.HTTPError as error:
        return CheckResult(
            "DELETE /v1/references/delete", False, http_error_detail(error)
        )

    def validate(response: httpx.Response) -> str | None:
        payload = response.json()
        if payload.get("success") is not True or payload.get("reference_id") != reference_id:
            return f"unexpected body: {json_detail(response)}"
        return None

    return result_from_response("DELETE /v1/references/delete", response, 200, validate)


async def cleanup_reference(client: httpx.AsyncClient, reference_id: str) -> None:
    try:
        await client.request(
            "DELETE",
            "/v1/references/delete",
            json={"reference_id": reference_id},
        )
    except httpx.HTTPError:
        pass


async def check_audio_speech(
    client: httpx.AsyncClient,
    voice_id: str | None,
    require_worker: bool,
    audio_dir: Path | None,
) -> CheckResult:
    payload = {
        "input": "fish-manager api availability test",
        "response_format": "wav",
        "stream": False,
    }
    add_reference_payload(payload, voice_id)
    try:
        response = await client.post("/v1/audio/speech", json=payload)
    except httpx.HTTPError as error:
        return CheckResult("POST /v1/audio/speech", False, http_error_detail(error))
    return inference_result(
        "POST /v1/audio/speech", response, require_worker, audio_dir
    )


async def check_fish_tts(
    client: httpx.AsyncClient,
    voice_id: str | None,
    require_worker: bool,
    audio_dir: Path | None,
) -> CheckResult:
    payload = {
        "text": "fish-manager api availability test",
        "format": "wav",
        "streaming": False,
    }
    if voice_id:
        payload["reference_id"] = voice_id
    else:
        payload["references"] = [reference_payload()]
    try:
        response = await client.post("/v1/tts", json=payload)
    except httpx.HTTPError as error:
        return CheckResult("POST /v1/tts", False, http_error_detail(error))
    return inference_result("POST /v1/tts", response, require_worker, audio_dir)


def add_reference_payload(payload: dict[str, Any], voice_id: str | None) -> None:
    if voice_id:
        payload["voice"] = voice_id
    else:
        payload["references"] = [reference_payload()]


def reference_payload() -> dict[str, str]:
    return {
        "text": "fish-manager api availability test reference",
        "content_type": "audio/wav",
        "audio_base64": tiny_wav_base64(),
    }


def inference_result(
    name: str,
    response: httpx.Response,
    require_worker: bool,
    audio_dir: Path | None,
) -> CheckResult:
    if response.status_code == 200:
        content_type = response.headers.get("content-type", "")
        size = len(response.content)
        body = None if is_binary_response(response) else response_body(response)
        saved_path = save_audio_response(name, response, audio_dir)
        detail = f"HTTP 200, content-type={content_type}, bytes={size}"
        if saved_path is not None:
            detail = f"{detail}, saved={saved_path}"
        return CheckResult(name, True, detail, response_body=body)
    if response.status_code == 429 and not require_worker:
        return CheckResult(
            name,
            True,
            "HTTP 429: endpoint is reachable, but no healthy worker is available",
            warning=True,
            response_body=response_body(response),
        )
    return CheckResult(
        name,
        False,
        f"HTTP {response.status_code}: {json_detail(response)}",
        response_body=response_body(response),
    )


async def check_worker_ws(
    base_url: str, worker_token: str | None, timeout: float
) -> CheckResult:
    name = "GET /internal/workers/ws"
    if not worker_token:
        return CheckResult(name, True, "WORKER_TOKEN not set; skipped", warning=True)

    ws_url = ws_url_from_base(base_url, worker_token)
    try:
        async with websockets.connect(
            ws_url, open_timeout=timeout, close_timeout=timeout
        ):
            return CheckResult(name, True, "websocket upgrade accepted")
    except Exception as error:  # websockets has version-specific exception classes.
        return CheckResult(name, False, str(error))


async def run_checks(args: argparse.Namespace) -> list[CheckResult]:
    headers = {"Authorization": f"Bearer {args.api_key}"} if args.api_key else {}
    timeout = httpx.Timeout(args.timeout)
    results: list[CheckResult] = []
    selected = args.selected_checks
    reference_id = f"api_test_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    updated_reference_id = f"{reference_id}_updated"
    active_reference_id = reference_id
    reference_created = False
    needs_reference = bool(selected & REFERENCE_CHECKS)

    async with httpx.AsyncClient(
        base_url=args.base_url.rstrip("/"),
        headers=headers,
        timeout=timeout,
        follow_redirects=False,
    ) as client:
        if "health" in selected:
            results.append(await check_health(client))
        if "workers" in selected:
            results.append(await check_workers(client))
        if "references-list" in selected:
            results.append(await check_list_references(client))

        if needs_reference:
            create_result, reference_created = await add_reference(client, reference_id)
            if "references-add" in selected:
                results.append(create_result)

        if needs_reference and not reference_created:
            skipped = []
            if "references-add" not in selected:
                skipped.append("POST /v1/references/add setup")
            if "references-update" in selected:
                skipped.append("POST /v1/references/update")
            if "references-delete" in selected:
                skipped.append("DELETE /v1/references/delete")
            results.extend(
                CheckResult(
                    name,
                    False,
                    "skipped because POST /v1/references/add setup failed",
                )
                for name in skipped
            )
            if "audio-speech" in selected:
                results.append(
                    await check_audio_speech(
                        client, None, args.require_worker, args.audio_dir
                    )
                )
            if "tts" in selected:
                results.append(
                    await check_fish_tts(
                        client, None, args.require_worker, args.audio_dir
                    )
                )
        else:
            try:
                if "references-update" in selected and reference_created:
                    results.append(
                        await check_update_reference(
                            client, reference_id, updated_reference_id
                        )
                    )
                    if results[-1].ok:
                        active_reference_id = updated_reference_id
                voice_ref = active_reference_id if reference_created else args.voice_id
                if "audio-speech" in selected:
                    results.append(
                        await check_audio_speech(
                            client, voice_ref, args.require_worker, args.audio_dir
                        )
                    )
                if "tts" in selected:
                    results.append(
                        await check_fish_tts(
                            client, voice_ref, args.require_worker, args.audio_dir
                        )
                    )
            finally:
                if "references-delete" in selected and reference_created:
                    results.append(await check_delete_reference(client, active_reference_id))
                    if results[-1].ok:
                        reference_created = False

        if reference_created:
            await cleanup_reference(client, active_reference_id)
            if active_reference_id != reference_id:
                await cleanup_reference(client, reference_id)

    if "worker-ws" in selected:
        results.append(
            await check_worker_ws(
                args.base_url.rstrip("/"), args.worker_token, args.timeout
            )
        )
    return results


def parse_selected_checks(
    values: list[str] | None, parser: argparse.ArgumentParser
) -> set[str]:
    if not values:
        return set(CHECK_ORDER)

    selected: set[str] = set()
    for value in values:
        for part in value.split(","):
            key = part.strip().lower()
            if not key:
                continue
            checks = CHECK_ALIASES.get(key)
            if checks is None:
                allowed = ", ".join(sorted(CHECK_ALIASES))
                parser.error(
                    f"unknown --only value {part!r}; allowed values: {allowed}"
                )
            selected.update(checks)

    return selected


def parse_args() -> argparse.Namespace:
    script_root = Path(__file__).resolve().parents[1]
    load_dotenv(script_root / ".env")

    parser = argparse.ArgumentParser(
        description="Check fish-manager API availability.",
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("FISH_MANAGER_BASE_URL", DEFAULT_BASE_URL),
        help=f"manager base URL, default: {DEFAULT_BASE_URL}",
    )
    parser.add_argument(
        "--api-key",
        default=first_api_key(),
        help="OpenAI-compatible API key; defaults to OPENAI_API_KEY or first OPENAI_API_KEYS",
    )
    parser.add_argument(
        "--worker-token",
        default=os.getenv("WORKER_TOKEN"),
        help="worker token for internal WebSocket check; defaults to WORKER_TOKEN",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=float(os.getenv("FISH_MANAGER_CHECK_TIMEOUT", "300")),
        help="per-request timeout in seconds, default: 300",
    )
    parser.add_argument(
        "--require-worker",
        action="store_true",
        help="fail TTS checks unless a healthy worker returns HTTP 200",
    )
    parser.add_argument(
        "--voice-id",
        "--reference-id",
        dest="voice_id",
        metavar="REFERENCE_ID",
        default=os.getenv(
            "FISH_MANAGER_CHECK_REFERENCE_ID",
            os.getenv("FISH_MANAGER_CHECK_VOICE_ID", "leijun"),
        ),
        help="existing voice/reference ID for speech checks when not creating a temporary reference, default: leijun",
    )
    parser.add_argument(
        "--audio-dir",
        default=os.getenv(
            "FISH_MANAGER_CHECK_AUDIO_DIR", str(script_root / "run" / "check_apis")
        ),
        help="directory for saved audio responses; set empty to disable saving",
    )
    parser.add_argument(
        "--only",
        action="append",
        metavar="CHECK",
        help=(
            "run only selected checks; repeat or comma-separate values. "
            "Common values: health, workers, references, references-list, references-add, "
            "references-update, references-delete, speech, audio-speech, tts, worker-ws, "
            "public, all"
        ),
    )
    args = parser.parse_args()
    args.selected_checks = parse_selected_checks(args.only, parser)
    args.audio_dir = Path(args.audio_dir) if args.audio_dir else None

    if args.selected_checks & AUTH_CHECKS and not args.api_key:
        parser.error(
            "--api-key is required unless OPENAI_API_KEY or OPENAI_API_KEYS is set"
        )
    return args


def print_results(results: list[CheckResult]) -> None:
    for result in results:
        if result.ok and result.warning:
            marker = "WARN"
        elif result.ok:
            marker = "PASS"
        else:
            marker = "FAIL"
        print(f"[{marker}] {result.name}: {result.detail}")
        if result.response_body:
            print("response:")
            print(result.response_body)

    failures = sum(1 for result in results if not result.ok)
    warnings = sum(1 for result in results if result.ok and result.warning)
    passed = len(results) - failures - warnings
    print(f"\nsummary: {passed} passed, {warnings} warnings, {failures} failed")


def main() -> int:
    args = parse_args()
    results = asyncio.run(run_checks(args))
    print_results(results)
    return 1 if any(not result.ok for result in results) else 0


if __name__ == "__main__":
    sys.exit(main())
