#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKER_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${WORKER_DIR}"

if [ ! -f .env ]; then
  echo "missing ${WORKER_DIR}/.env; run scripts/autodl_setup.sh first" >&2
  exit 1
fi

set -a
source .env
set +a

export PATH="${WORKER_DIR}/.venv/bin:${PATH}"
export PYTHONUNBUFFERED=1
export HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"

MODEL_ID="${MODEL_ID:-fishaudio/s2-pro}"
MODEL_DIR="${MODEL_DIR:-/root/autodl-fs/models}"
CACHE_DIR="${CACHE_DIR:-/root/autodl-tmp/cache/fish-worker}"

if [ "${MANAGER_URL:-}" = "wss://manager.example.com/internal/workers/ws" ] || [ -z "${MANAGER_URL:-}" ]; then
  echo "MANAGER_URL is not configured in ${WORKER_DIR}/.env" >&2
  exit 1
fi

if [ "${WORKER_TOKEN:-}" = "replace-me" ] || [ -z "${WORKER_TOKEN:-}" ]; then
  echo "WORKER_TOKEN is not configured in ${WORKER_DIR}/.env" >&2
  exit 1
fi

mkdir -p "${MODEL_DIR}" "${CACHE_DIR}/refs"

if [ ! -d "${MODEL_DIR}" ] || [ -z "$(ls -A "${MODEL_DIR}" 2>/dev/null || true)" ]; then
  echo "downloading ${MODEL_ID} to ${MODEL_DIR} via ${HF_ENDPOINT}"
  if command -v hf >/dev/null 2>&1; then
    hf download "${MODEL_ID}" --local-dir "${MODEL_DIR}"
  else
    huggingface-cli download "${MODEL_ID}" --local-dir "${MODEL_DIR}"
  fi
fi

exec "${WORKER_DIR}/.venv/bin/fish-worker"
