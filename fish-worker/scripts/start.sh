#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${PROJECT_DIR}/.." && pwd)"

cd "${REPO_ROOT}"

normalize_thread_env() {
  case "${OMP_NUM_THREADS:-}" in
    ''|*[!0-9]*|0) export OMP_NUM_THREADS=1 ;;
  esac
}

if [ -z "${MODEL_DIR+x}" ]; then
  export MODEL_DIR=/models/s2-pro
fi
if [ -z "${API_SERVER_HOST+x}" ]; then
  export API_SERVER_HOST=0.0.0.0
fi
if [ -z "${API_SERVER_PORT+x}" ]; then
  export API_SERVER_PORT=8000
fi
if [ -z "${API_SERVER_DECODER_CHECKPOINT_PATH+x}" ]; then
  export API_SERVER_DECODER_CHECKPOINT_PATH="${MODEL_DIR}/codec.pth"
fi
if [ -z "${API_SERVER_DECODER_CONFIG_NAME+x}" ]; then
  export API_SERVER_DECODER_CONFIG_NAME=modded_dac_vq
fi
if [ -z "${API_SERVER_DECODER_DTYPE+x}" ]; then
  export API_SERVER_DECODER_DTYPE=float32
fi
if [ -z "${API_SERVER_MAX_RUNNING_REQUESTS+x}" ]; then
  export API_SERVER_MAX_RUNNING_REQUESTS=1
fi
if [ -z "${API_SERVER_MAX_QUEUED_REQUESTS+x}" ]; then
  export API_SERVER_MAX_QUEUED_REQUESTS=1
fi
if [ -z "${API_SERVER_TTS_MAX_NEW_TOKENS+x}" ]; then
  export API_SERVER_TTS_MAX_NEW_TOKENS=4096
fi
# Worker deployment pins the LLAMA context cap to 8192 (multiple of 8) to cut
# the one-time 32768-token KV cache + causal-mask VRAM. To keep the
# checkpoint's original cap, explicitly set it to an empty string:
#   API_SERVER_LLAMA_MAX_SEQ_LEN=
if [ -z "${API_SERVER_LLAMA_MAX_SEQ_LEN+x}" ]; then
  export API_SERVER_LLAMA_MAX_SEQ_LEN=8192
fi
if [ -z "${API_SERVER_COMPILE+x}" ]; then
  export API_SERVER_COMPILE=1
fi
if [ -z "${API_SERVER_HALF+x}" ]; then
  export API_SERVER_HALF=0
fi
if [ -z "${API_SERVER_WORKERS+x}" ]; then
  export API_SERVER_WORKERS=1
fi
if [ -z "${API_SERVER_MAX_TEXT_LENGTH+x}" ]; then
  export API_SERVER_MAX_TEXT_LENGTH=0
fi
if [ -z "${API_SERVER_SPEED_METHOD+x}" ]; then
  export API_SERVER_SPEED_METHOD=librosa
fi
if [ -z "${API_SERVER_REFERENCES_DIR+x}" ]; then
  export API_SERVER_REFERENCES_DIR=references
fi
if [ -z "${PYTORCH_ALLOC_CONF+x}" ]; then
  export PYTORCH_ALLOC_CONF=expandable_segments:True
fi

normalize_thread_env
bash "${SCRIPT_DIR}/ensure_model.sh"

if [ -x "${PROJECT_DIR}/.venv/bin/fish-worker" ]; then
  exec "${PROJECT_DIR}/.venv/bin/fish-worker"
fi

exec fish-worker
