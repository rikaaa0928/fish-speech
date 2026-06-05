#!/usr/bin/env bash
set -euo pipefail

export HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"
MODEL_ID="${MODEL_ID:-fishaudio/s2-pro}"
MODEL_DIR="${MODEL_DIR:-/models/s2-pro}"

if [ -d "${MODEL_DIR}" ] && [ -n "$(ls -A "${MODEL_DIR}" 2>/dev/null || true)" ]; then
  echo "model already exists at ${MODEL_DIR}"
  exit 0
fi

mkdir -p "${MODEL_DIR}"
echo "downloading ${MODEL_ID} to ${MODEL_DIR} via ${HF_ENDPOINT}"

if uv run hf --help >/dev/null 2>&1; then
  uv run hf download "${MODEL_ID}" --local-dir "${MODEL_DIR}"
else
  uv run huggingface-cli download "${MODEL_ID}" --local-dir "${MODEL_DIR}"
fi
