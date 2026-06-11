#!/usr/bin/env bash
set -euo pipefail

export PATH="${HOME}/.local/bin:${PATH}"
export HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"
MODEL_ID="${MODEL_ID:-fishaudio/s2-pro}"
MODEL_DIR="${MODEL_DIR:-/autodl-fs/data/models/s2-pro}"
MODEL_REQUIRED_FILES="${MODEL_REQUIRED_FILES:-codec.pth model-00001-of-00002.safetensors model-00002-of-00002.safetensors}"
HFD_SCRIPT="${HFD_SCRIPT:-/root/autodl-tmp/cache/hfd.sh}"
HFD_URL="${HFD_URL:-https://hf-mirror.com/hfd/hfd.sh}"
HFD_TOOL="${HFD_TOOL:-aria2c}"
HFD_THREADS="${HFD_THREADS:-8}"
HFD_EXTRA_ARGS="${HFD_EXTRA_ARGS:-}"

print_model_state() {
  echo "model dir: ${MODEL_DIR}"
  echo "required model files: ${MODEL_REQUIRED_FILES}"

  if [ ! -d "${MODEL_DIR}" ]; then
    echo "model dir does not exist yet"
    return 0
  fi

  if [ -z "$(ls -A "${MODEL_DIR}" 2>/dev/null || true)" ]; then
    echo "model dir is empty"
  fi

  for file in ${MODEL_REQUIRED_FILES}; do
    if [ ! -e "${MODEL_DIR}/${file}" ]; then
      echo "missing required model file: ${file}"
    elif [ ! -s "${MODEL_DIR}/${file}" ]; then
      echo "empty required model file: ${file}"
    fi

    if [ -e "${MODEL_DIR}/${file}.aria2" ]; then
      echo "aria2 state file exists for ${file}: ${MODEL_DIR}/${file}.aria2"
    fi
  done
}

model_complete() {
  if [ ! -d "${MODEL_DIR}" ] || [ -z "$(ls -A "${MODEL_DIR}" 2>/dev/null || true)" ]; then
    return 1
  fi

  for file in ${MODEL_REQUIRED_FILES}; do
    if [ ! -s "${MODEL_DIR}/${file}" ]; then
      return 1
    fi
  done

  return 0
}

print_model_state
if model_complete; then
  echo "model already exists at ${MODEL_DIR}"
  exit 0
fi

echo "model is incomplete at ${MODEL_DIR}; starting download"

mkdir -p "${MODEL_DIR}"
mkdir -p "$(dirname "${HFD_SCRIPT}")"

if [ ! -s "${HFD_SCRIPT}" ]; then
  echo "downloading hfd.sh to ${HFD_SCRIPT}"
  if command -v curl >/dev/null 2>&1; then
    curl -L --retry 3 --connect-timeout 20 -o "${HFD_SCRIPT}" "${HFD_URL}"
  elif command -v wget >/dev/null 2>&1; then
    wget -O "${HFD_SCRIPT}" "${HFD_URL}"
  else
    printf 'curl or wget is required to download hfd.sh.\n' >&2
    exit 1
  fi
fi
chmod +x "${HFD_SCRIPT}"

echo "downloading ${MODEL_ID} to ${MODEL_DIR} via hfd.sh (${HF_ENDPOINT})"
# shellcheck disable=SC2086
bash "${HFD_SCRIPT}" "${MODEL_ID}" --local-dir "${MODEL_DIR}" --tool "${HFD_TOOL}" -x "${HFD_THREADS}" ${HFD_EXTRA_ARGS}

if ! model_complete; then
  printf 'model download did not produce required files in %s:\n' "${MODEL_DIR}" >&2
  for file in ${MODEL_REQUIRED_FILES}; do
    if [ ! -s "${MODEL_DIR}/${file}" ]; then
      printf '  missing or empty: %s\n' "${file}" >&2
    fi
  done
  exit 1
fi

echo "model is ready at ${MODEL_DIR}"
