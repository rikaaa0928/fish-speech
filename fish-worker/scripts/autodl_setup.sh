#!/usr/bin/env bash
set -euo pipefail

# AutoDL bare-metal setup for the PyTorch 2.8 / CUDA 12.8 image.
#
# Typical usage on the AutoDL GPU host:
#   MANAGER_URL=wss://manager.example.com/internal/workers/ws \
#   WORKER_TOKEN=replace-me \
#   bash fish-worker/scripts/autodl_setup.sh
#
# If you only copied this script and need it to clone the repository first:
#   REPO_URL=https://github.com/you/fish-speech.git \
#   REPO_REF=main \
#   MANAGER_URL=wss://manager.example.com/internal/workers/ws \
#   WORKER_TOKEN=replace-me \
#   bash autodl_setup.sh

log() {
  printf '[autodl] %s\n' "$*"
}

warn() {
  printf '[autodl] warning: %s\n' "$*" >&2
}

fail() {
  printf '[autodl] error: %s\n' "$*" >&2
  exit 1
}

run_as_root() {
  if [ "${EUID:-$(id -u)}" -eq 0 ]; then
    "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  else
    fail "need root or sudo to run: $*"
  fi
}

BASE_PACKAGES_INSTALLED=0
install_base_packages() {
  if [ "${BASE_PACKAGES_INSTALLED}" = "1" ]; then
    return
  fi

  if command -v apt-get >/dev/null 2>&1; then
    log "installing base system packages"
    run_as_root apt-get update
    run_as_root apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      ffmpeg \
      git \
      git-lfs \
      libsndfile1 \
      build-essential \
      pkg-config
    git lfs install --skip-repo || true
    BASE_PACKAGES_INSTALLED=1
  else
    warn "apt-get not found; skipping system package installation"
    BASE_PACKAGES_INSTALLED=1
  fi
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKER_DIR="$(cd "${SCRIPT_DIR}/.." 2>/dev/null && pwd || true)"
INSTALL_ROOT="${INSTALL_ROOT:-/root/autodl-tmp/fish-speech}"

if [ ! -f "${WORKER_DIR}/pyproject.toml" ]; then
  if [ -z "${REPO_URL:-}" ]; then
    fail "fish-worker not found. Run this inside the repo, or set REPO_URL to clone it."
  fi

  install_base_packages
  log "cloning ${REPO_URL} into ${INSTALL_ROOT}"
  mkdir -p "$(dirname "${INSTALL_ROOT}")"
  if [ -d "${INSTALL_ROOT}/.git" ]; then
    git -C "${INSTALL_ROOT}" fetch --all --prune
  else
    git clone "${REPO_URL}" "${INSTALL_ROOT}"
  fi
  if [ -n "${REPO_REF:-}" ]; then
    git -C "${INSTALL_ROOT}" checkout "${REPO_REF}"
  fi
  WORKER_DIR="${INSTALL_ROOT}/fish-worker"
fi

[ -f "${WORKER_DIR}/pyproject.toml" ] || fail "${WORKER_DIR}/pyproject.toml not found"
PROJECT_ROOT="$(cd "${WORKER_DIR}/.." && pwd)"
VENV_DIR="${VENV_DIR:-${WORKER_DIR}/.venv}"
PYTHON_VERSION="${PYTHON_VERSION:-3.11}"
PYTORCH_INDEX_URL="${PYTORCH_INDEX_URL:-https://download.pytorch.org/whl/cu128}"
TORCH_VERSION="${TORCH_VERSION:-2.8.0}"
TORCHVISION_VERSION="${TORCHVISION_VERSION:-0.23.0}"
TORCHAUDIO_VERSION="${TORCHAUDIO_VERSION:-2.8.0}"
SGLANG_INSTALL_SPEC="${SGLANG_INSTALL_SPEC:-sglang[all]}"
INSTALL_SGLANG="${INSTALL_SGLANG:-1}"
INSTALL_FLASHINFER="${INSTALL_FLASHINFER:-0}"
FLASHINFER_INDEX_URL="${FLASHINFER_INDEX_URL:-https://flashinfer.ai/whl/cu128/torch2.8/}"

log "worker dir: ${WORKER_DIR}"
install_base_packages

if ! command -v uv >/dev/null 2>&1; then
  log "installing uv"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="${HOME}/.local/bin:${PATH}"
fi
command -v uv >/dev/null 2>&1 || fail "uv is not on PATH after installation"

if command -v nvidia-smi >/dev/null 2>&1; then
  log "detected GPU"
  nvidia-smi
else
  warn "nvidia-smi not found; AutoDL GPU runtime may not be attached"
fi

if python3 - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY
then
  PYTHON_BIN="${PYTHON_BIN:-python3}"
else
  log "installing Python ${PYTHON_VERSION} via uv"
  uv python install "${PYTHON_VERSION}"
  PYTHON_BIN="${PYTHON_BIN:-${PYTHON_VERSION}}"
fi

log "creating venv at ${VENV_DIR}"
uv venv "${VENV_DIR}" --python "${PYTHON_BIN}"
VENV_PYTHON="${VENV_DIR}/bin/python"

log "installing fish-worker dependencies"
(
  cd "${WORKER_DIR}"
  UV_PROJECT_ENVIRONMENT="${VENV_DIR}" uv sync --no-dev --inexact
)

log "installing PyTorch ${TORCH_VERSION} CUDA 12.8 wheels"
uv pip install --python "${VENV_PYTHON}" --upgrade pip setuptools wheel packaging ninja
uv pip install --python "${VENV_PYTHON}" --index-url "${PYTORCH_INDEX_URL}" \
  "torch==${TORCH_VERSION}" \
  "torchvision==${TORCHVISION_VERSION}" \
  "torchaudio==${TORCHAUDIO_VERSION}"

if [ "${INSTALL_FLASHINFER}" = "1" ]; then
  log "installing flashinfer-python from ${FLASHINFER_INDEX_URL}"
  uv pip install --python "${VENV_PYTHON}" --index-url "${FLASHINFER_INDEX_URL}" flashinfer-python || \
    warn "flashinfer-python install failed; continuing without it"
fi

if [ "${INSTALL_SGLANG}" = "1" ]; then
  log "installing ${SGLANG_INSTALL_SPEC}"
  uv pip install --python "${VENV_PYTHON}" --upgrade "${SGLANG_INSTALL_SPEC}"
fi

log "validating torch"
"${VENV_PYTHON}" - <<'PY'
import torch
print(f"torch={torch.__version__}")
print(f"torch_cuda={torch.version.cuda}")
print(f"cuda_available={torch.cuda.is_available()}")
if not torch.__version__.startswith("2.8.0"):
    raise SystemExit("expected torch 2.8.0")
if torch.version.cuda != "12.8":
    raise SystemExit("expected torch CUDA 12.8")
PY

if [ "${INSTALL_SGLANG}" = "1" ] && [ ! -x "${VENV_DIR}/bin/sgl-omni" ]; then
  warn "sgl-omni was not found in ${VENV_DIR}/bin; set SGLANG_COMMAND or adjust SGLANG_INSTALL_SPEC if startup fails"
fi

MODEL_ID="${MODEL_ID:-fishaudio/s2-pro}"
MODEL_DIR="${MODEL_DIR:-/root/autodl-fs/models}"
CACHE_DIR="${CACHE_DIR:-/root/autodl-tmp/cache/fish-worker}"
HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"
SGLANG_HOST="${SGLANG_HOST:-127.0.0.1}"
SGLANG_PORT="${SGLANG_PORT:-8000}"
SGLANG_MAX_RUNNING_REQUESTS="${SGLANG_MAX_RUNNING_REQUESTS:-4}"
SGLANG_MAX_QUEUED_REQUESTS="${SGLANG_MAX_QUEUED_REQUESTS:-2}"
WORKER_MAX_INFLIGHT="${WORKER_MAX_INFLIGHT:-${SGLANG_MAX_RUNNING_REQUESTS}}"
WORKER_MAX_QUEUE="${WORKER_MAX_QUEUE:-${SGLANG_MAX_QUEUED_REQUESTS}}"
WORKER_MANAGE_SGLANG="${WORKER_MANAGE_SGLANG:-1}"
HEARTBEAT_INTERVAL_SECONDS="${HEARTBEAT_INTERVAL_SECONDS:-5}"

if [ -n "${SGLANG_CONFIG:-}" ]; then
  SGLANG_CONFIG_VALUE="${SGLANG_CONFIG}"
else
  SGLANG_CONFIG_VALUE="$(find "${PROJECT_ROOT}" "${VENV_DIR}" -name s2pro_tts.yaml -print -quit 2>/dev/null || true)"
fi

if [ -z "${SGLANG_CONFIG_VALUE}" ]; then
  warn "s2pro_tts.yaml was not found; .env will leave SGLANG_CONFIG empty and rely on sgl-omni defaults"
fi

mkdir -p "${MODEL_DIR}" "${CACHE_DIR}"

log "writing ${WORKER_DIR}/.env"
cat > "${WORKER_DIR}/.env" <<EOF
MANAGER_URL=${MANAGER_URL:-wss://manager.example.com/internal/workers/ws}
WORKER_TOKEN=${WORKER_TOKEN:-replace-me}
WORKER_ID=${WORKER_ID:-}

HF_ENDPOINT=${HF_ENDPOINT}
MODEL_ID=${MODEL_ID}
MODEL_DIR=${MODEL_DIR}
CACHE_DIR=${CACHE_DIR}

SGLANG_HOST=${SGLANG_HOST}
SGLANG_PORT=${SGLANG_PORT}
SGLANG_CONFIG=${SGLANG_CONFIG_VALUE}
SGLANG_MAX_RUNNING_REQUESTS=${SGLANG_MAX_RUNNING_REQUESTS}
SGLANG_MAX_QUEUED_REQUESTS=${SGLANG_MAX_QUEUED_REQUESTS}
SGLANG_STARTUP_TIMEOUT_SECONDS=${SGLANG_STARTUP_TIMEOUT_SECONDS:-900}

WORKER_MAX_INFLIGHT=${WORKER_MAX_INFLIGHT}
WORKER_MAX_QUEUE=${WORKER_MAX_QUEUE}
WORKER_MANAGE_SGLANG=${WORKER_MANAGE_SGLANG}
HEARTBEAT_INTERVAL_SECONDS=${HEARTBEAT_INTERVAL_SECONDS}
EOF
chmod 600 "${WORKER_DIR}/.env"

if [ "${DOWNLOAD_MODEL:-1}" = "1" ]; then
  log "downloading ${MODEL_ID} to ${MODEL_DIR} via ${HF_ENDPOINT}"
  export HF_ENDPOINT
  if [ -x "${VENV_DIR}/bin/hf" ]; then
    "${VENV_DIR}/bin/hf" download "${MODEL_ID}" --local-dir "${MODEL_DIR}"
  else
    "${VENV_DIR}/bin/huggingface-cli" download "${MODEL_ID}" --local-dir "${MODEL_DIR}"
  fi
else
  warn "DOWNLOAD_MODEL=0; skipping model download"
fi

log "setup complete"
cat <<EOF

Next commands:
  cd ${WORKER_DIR}
  bash scripts/autodl_start.sh

Before starting, edit ${WORKER_DIR}/.env if MANAGER_URL or WORKER_TOKEN still uses placeholders.
EOF
