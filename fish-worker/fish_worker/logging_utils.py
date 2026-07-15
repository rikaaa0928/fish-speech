from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any


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
