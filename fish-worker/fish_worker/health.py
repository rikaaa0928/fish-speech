from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from typing import Any


GPU_FATAL_EXIT_CODE = 70

CUDA_TINY_OP_CODE = """
import torch

if not torch.cuda.is_available():
    raise SystemExit("torch.cuda.is_available() is False")
if torch.cuda.device_count() < 1:
    raise SystemExit("torch.cuda.device_count() is 0")
torch.empty(1, device="cuda").sum().item()
"""


@dataclass(frozen=True)
class HealthCheck:
    healthy: bool
    detail: str | None
    gpu: dict[str, Any]
    gpu_failure: bool = False
    api_server_failure: bool = False


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
    except Exception as exc:  # noqa: BLE001
        return {"gpu_count": 0, "error": f"{type(exc).__name__}: {exc}"}

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


def run_cuda_tiny_op(timeout: float) -> tuple[bool, str | None]:
    try:
        subprocess.check_output(
            [sys.executable, "-c", CUDA_TINY_OP_CODE],
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
        )
        return True, None
    except subprocess.TimeoutExpired as exc:
        return False, f"timeout after {timeout:.1f}s: {trim_output(exc.output)}"
    except subprocess.CalledProcessError as exc:
        return False, trim_output(exc.output)
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def trim_output(output: str | bytes | None, limit: int = 500) -> str:
    if output is None:
        return ""
    if isinstance(output, bytes):
        output = output.decode(errors="replace")
    output = " ".join(output.split())
    return output[:limit]
