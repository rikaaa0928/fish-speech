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
    "voices-list",
    "voices-create",
    "voices-get",
    "audio-speech",
    "tts",
    "voices-delete",
    "worker-ws",
]
VOICE_CHECKS = {"voices-create", "voices-get", "voices-delete"}
SPEECH_CHECKS = {"audio-speech", "tts"}
PUBLIC_CHECKS = set(CHECK_ORDER) - {"worker-ws"}
AUTH_CHECKS = PUBLIC_CHECKS - {"health"}
CHECK_ALIASES = {
    "all": set(CHECK_ORDER),
    "public": PUBLIC_CHECKS,
    "voices": {"voices-list", "voices-create", "voices-get", "voices-delete"},
    "speech": SPEECH_CHECKS,
    "health": {"health"},
    "/health": {"health"},
    "get /health": {"health"},
    "workers": {"workers"},
    "/v1/workers": {"workers"},
    "get /v1/workers": {"workers"},
    "voices-list": {"voices-list"},
    "list-voices": {"voices-list"},
    "get /v1/voices": {"voices-list"},
    "voices-create": {"voices-create"},
    "create-voice": {"voices-create"},
    "post /v1/voices": {"voices-create"},
    "voices-get": {"voices-get"},
    "get-voice": {"voices-get"},
    "get /v1/voices/{voice_id}": {"voices-get"},
    "voices-delete": {"voices-delete"},
    "delete-voice": {"voices-delete"},
    "delete /v1/voices/{voice_id}": {"voices-delete"},
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
    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    return content_type.startswith("audio/") or content_type == "application/octet-stream"


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
            return CheckResult(name, False, error, response_body=response_body(response))
    return CheckResult(name, True, f"HTTP {response.status_code}", response_body=response_body(response))


async def check_health(client: httpx.AsyncClient) -> CheckResult:
    try:
        response = await client.get("/health")
    except httpx.HTTPError as error:
        return CheckResult("GET /health", False, str(error))

    def validate(response: httpx.Response) -> str | None:
        if response.json().get("status") != "ok":
            return f"unexpected body: {json_detail(response)}"
        return None

    return result_from_response("GET /health", response, 200, validate)


async def check_workers(client: httpx.AsyncClient) -> CheckResult:
    try:
        response = await client.get("/v1/workers")
    except httpx.HTTPError as error:
        return CheckResult("GET /v1/workers", False, str(error))

    def validate(response: httpx.Response) -> str | None:
        data = response.json().get("data")
        if not isinstance(data, list):
            return f"expected data list, got: {json_detail(response)}"
        return None

    return result_from_response("GET /v1/workers", response, 200, validate)


async def check_list_voices(client: httpx.AsyncClient) -> CheckResult:
    try:
        response = await client.get("/v1/voices", params={"limit": 1})
    except httpx.HTTPError as error:
        return CheckResult("GET /v1/voices", False, str(error))

    def validate(response: httpx.Response) -> str | None:
        payload = response.json()
        if not isinstance(payload.get("voices"), list):
            return f"expected voices list, got: {json_detail(response)}"
        return None

    return result_from_response("GET /v1/voices", response, 200, validate)


async def create_voice(client: httpx.AsyncClient, voice_id: str) -> tuple[CheckResult, bool]:
    payload = {
        "voice_id": voice_id,
        "text": "fish-manager api availability test reference",
        "content_type": "audio/wav",
        "audio_base64": tiny_wav_base64(),
    }
    try:
        response = await client.post("/v1/voices", json=payload)
    except httpx.HTTPError as error:
        return CheckResult("POST /v1/voices", False, str(error)), False

    def validate(response: httpx.Response) -> str | None:
        payload = response.json()
        if payload.get("voice_id") != voice_id:
            return f"unexpected voice_id: {json_detail(response)}"
        return None

    result = result_from_response("POST /v1/voices", response, 200, validate)
    return result, result.ok


async def check_get_voice(client: httpx.AsyncClient, voice_id: str) -> CheckResult:
    try:
        response = await client.get(f"/v1/voices/{voice_id}")
    except httpx.HTTPError as error:
        return CheckResult("GET /v1/voices/{voice_id}", False, str(error))

    def validate(response: httpx.Response) -> str | None:
        if response.json().get("voice_id") != voice_id:
            return f"unexpected body: {json_detail(response)}"
        return None

    return result_from_response("GET /v1/voices/{voice_id}", response, 200, validate)


async def check_delete_voice(client: httpx.AsyncClient, voice_id: str) -> CheckResult:
    try:
        response = await client.delete(f"/v1/voices/{voice_id}")
    except httpx.HTTPError as error:
        return CheckResult("DELETE /v1/voices/{voice_id}", False, str(error))
    return result_from_response("DELETE /v1/voices/{voice_id}", response, 204)


async def cleanup_voice(client: httpx.AsyncClient, voice_id: str) -> None:
    try:
        await client.delete(f"/v1/voices/{voice_id}")
    except httpx.HTTPError:
        pass


async def check_audio_speech(
    client: httpx.AsyncClient,
    voice_id: str | None,
    require_worker: bool,
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
        return CheckResult("POST /v1/audio/speech", False, str(error))
    return inference_result("POST /v1/audio/speech", response, require_worker)


async def check_fish_tts(
    client: httpx.AsyncClient,
    voice_id: str | None,
    require_worker: bool,
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
        return CheckResult("POST /v1/tts", False, str(error))
    return inference_result("POST /v1/tts", response, require_worker)


def add_reference_payload(payload: dict[str, Any], voice_id: str | None) -> None:
    if voice_id:
        payload["voice_id"] = voice_id
    else:
        payload["references"] = [reference_payload()]


def reference_payload() -> dict[str, str]:
    return {
        "text": "fish-manager api availability test reference",
        "content_type": "audio/wav",
        "audio_base64": tiny_wav_base64(),
    }


def inference_result(name: str, response: httpx.Response, require_worker: bool) -> CheckResult:
    if response.status_code == 200:
        content_type = response.headers.get("content-type", "")
        size = len(response.content)
        body = None if is_binary_response(response) else response_body(response)
        return CheckResult(name, True, f"HTTP 200, content-type={content_type}, bytes={size}", response_body=body)
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


async def check_worker_ws(base_url: str, worker_token: str | None, timeout: float) -> CheckResult:
    name = "GET /internal/workers/ws"
    if not worker_token:
        return CheckResult(name, True, "WORKER_TOKEN not set; skipped", warning=True)

    ws_url = ws_url_from_base(base_url, worker_token)
    try:
        async with websockets.connect(ws_url, open_timeout=timeout, close_timeout=timeout):
            return CheckResult(name, True, "websocket upgrade accepted")
    except Exception as error:  # websockets has version-specific exception classes.
        return CheckResult(name, False, str(error))


async def run_checks(args: argparse.Namespace) -> list[CheckResult]:
    headers = {"Authorization": f"Bearer {args.api_key}"} if args.api_key else {}
    timeout = httpx.Timeout(args.timeout)
    results: list[CheckResult] = []
    selected = args.selected_checks
    voice_id = f"api_test_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    voice_created = False
    needs_voice = bool(selected & VOICE_CHECKS)

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
        if "voices-list" in selected:
            results.append(await check_list_voices(client))

        if needs_voice:
            create_result, voice_created = await create_voice(client, voice_id)
            if "voices-create" in selected:
                results.append(create_result)

        if needs_voice and not voice_created:
            skipped = []
            if "voices-create" not in selected:
                skipped.append("POST /v1/voices setup")
            if "voices-get" in selected:
                skipped.append("GET /v1/voices/{voice_id}")
            if "voices-delete" in selected:
                skipped.append("DELETE /v1/voices/{voice_id}")
            results.extend(
                CheckResult(name, False, "skipped because POST /v1/voices setup failed")
                for name in skipped
            )
            if "audio-speech" in selected:
                results.append(await check_audio_speech(client, None, args.require_worker))
            if "tts" in selected:
                results.append(await check_fish_tts(client, None, args.require_worker))
        else:
            try:
                voice_ref = voice_id if voice_created else None
                if "voices-get" in selected:
                    results.append(await check_get_voice(client, voice_id))
                if "audio-speech" in selected:
                    results.append(await check_audio_speech(client, voice_ref, args.require_worker))
                if "tts" in selected:
                    results.append(await check_fish_tts(client, voice_ref, args.require_worker))
            finally:
                if "voices-delete" in selected and voice_created:
                    results.append(await check_delete_voice(client, voice_id))

        if voice_created:
            await cleanup_voice(client, voice_id)

    if "worker-ws" in selected:
        results.append(await check_worker_ws(args.base_url.rstrip("/"), args.worker_token, args.timeout))
    return results


def parse_selected_checks(values: list[str] | None, parser: argparse.ArgumentParser) -> set[str]:
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
                parser.error(f"unknown --only value {part!r}; allowed values: {allowed}")
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
        default=float(os.getenv("FISH_MANAGER_CHECK_TIMEOUT", "10")),
        help="per-request timeout in seconds, default: 10",
    )
    parser.add_argument(
        "--require-worker",
        action="store_true",
        help="fail TTS checks unless a healthy worker returns HTTP 200",
    )
    parser.add_argument(
        "--only",
        action="append",
        metavar="CHECK",
        help=(
            "run only selected checks; repeat or comma-separate values. "
            "Common values: health, workers, voices, voices-list, voices-create, "
            "voices-get, voices-delete, speech, audio-speech, tts, worker-ws, public, all"
        ),
    )
    args = parser.parse_args()
    args.selected_checks = parse_selected_checks(args.only, parser)

    if args.selected_checks & AUTH_CHECKS and not args.api_key:
        parser.error("--api-key is required unless OPENAI_API_KEY or OPENAI_API_KEYS is set")
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
