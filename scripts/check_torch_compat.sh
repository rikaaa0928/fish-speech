#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -gt 1 ]; then
  printf 'Usage: %s [python-bin]\n' "$0" >&2
  printf '       PYTHON_BIN=/path/to/python %s\n' "$0" >&2
  exit 2
fi

PYTHON_BIN="${PYTHON_BIN:-}"
if [ "$#" -eq 1 ]; then
  PYTHON_BIN="$1"
fi

if [ -z "${PYTHON_BIN}" ]; then
  if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="python3"
  elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN="python"
  else
    printf 'No python found. Pass one explicitly: %s /path/to/python\n' "$0" >&2
    exit 2
  fi
fi

if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
  printf 'Python binary not found: %s\n' "${PYTHON_BIN}" >&2
  exit 2
fi

"${PYTHON_BIN}" - <<'PY'
import re
import sys

try:
    import torch
except Exception as exc:
    print("PyTorch: missing or failed to import")
    print(f"Error: {exc}")
    raise SystemExit(1)

version = torch.__version__
match = re.match(r"^(\d+)\.(\d+)", version)
major_minor = tuple(int(part) for part in match.groups()) if match else None
cuda_version = torch.version.cuda or ""
cuda_available = torch.cuda.is_available()

version_ok = major_minor in {(2, 8), (2, 9)}
cuda_ok = cuda_version.startswith("12.8")
compatible = version_ok and cuda_ok and cuda_available

print(f"Python: {sys.executable}")
print(f"PyTorch: {version}")
print(f"PyTorch CUDA build: {cuda_version or 'none'}")
print(f"CUDA available: {cuda_available}")

if cuda_available:
    print(f"GPU count: {torch.cuda.device_count()}")
    for index in range(torch.cuda.device_count()):
        print(f"GPU {index}: {torch.cuda.get_device_name(index)}")

print()
if compatible:
    print("Result: compatible")
    raise SystemExit(0)

print("Result: incompatible")
if not version_ok:
    print("- Expected PyTorch 2.8.x or 2.9.x")
if not cuda_ok:
    print("- Expected a CUDA 12.8 PyTorch build")
if not cuda_available:
    print("- Expected torch.cuda.is_available() == True")
raise SystemExit(1)
PY
