from __future__ import annotations

from typing import Any


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
