from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit, urlunsplit

import msgpack


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
