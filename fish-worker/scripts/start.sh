#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${PROJECT_DIR}"

prepare_sglang_config() {
  if [ "${WORKER_MANAGE_SGLANG:-1}" = "0" ] || [ -z "${SGLANG_CONFIG:-}" ] || [ ! -f "${SGLANG_CONFIG}" ]; then
    return
  fi

  local model_dir="${MODEL_DIR:-/models/s2-pro}"
  local generated_dir="${PROJECT_DIR}/.generated"
  local generated_config="${generated_dir}/$(basename "${SGLANG_CONFIG}")"
  local source_config
  local has_model_path=0

  mkdir -p "${generated_dir}"
  source_config="$(cd "$(dirname "${SGLANG_CONFIG}")" && pwd)/$(basename "${SGLANG_CONFIG}")"
  generated_config="$(cd "${generated_dir}" && pwd)/$(basename "${SGLANG_CONFIG}")"
  if [ "${source_config}" = "${generated_config}" ]; then
    export SGLANG_CONFIG="${generated_config}"
    return 0
  fi

  local tmp_config="${generated_config}.tmp.$$"
  while IFS= read -r line; do
    case "${line}" in
      model_path:*)
        printf 'model_path: %s\n' "${model_dir}"
        has_model_path=1
        ;;
      *)
        printf '%s\n' "${line}"
        ;;
    esac
  done <"${SGLANG_CONFIG}" >"${tmp_config}"

  if [ "${has_model_path}" = "0" ]; then
    printf 'model_path: %s\n' "${model_dir}" >>"${tmp_config}"
  fi

  if [ -n "${SGLANG_TTS_MEM_FRACTION_STATIC:-}" ] || [ -n "${SGLANG_TTS_MAX_RUNNING_REQUESTS:-}" ] || [ -n "${SGLANG_TTS_MAX_NEW_TOKENS:-}" ] || [ -n "${SGLANG_TTS_TORCH_COMPILE:-}" ] || [ -n "${SGLANG_TTS_CUDA_GRAPH:-}" ]; then
    {
      printf 'runtime_overrides:\n'
      printf '  tts_engine:\n'
      if [ -n "${SGLANG_TTS_MAX_NEW_TOKENS:-}" ]; then
        printf '    max_new_tokens: %s\n' "${SGLANG_TTS_MAX_NEW_TOKENS}"
      fi
      if [ -n "${SGLANG_TTS_MEM_FRACTION_STATIC:-}" ] || [ -n "${SGLANG_TTS_MAX_RUNNING_REQUESTS:-}" ] || [ -n "${SGLANG_TTS_TORCH_COMPILE:-}" ] || [ -n "${SGLANG_TTS_CUDA_GRAPH:-}" ]; then
        printf '    server_args_overrides:\n'
        if [ -n "${SGLANG_TTS_MEM_FRACTION_STATIC:-}" ]; then
          printf '      mem_fraction_static: %s\n' "${SGLANG_TTS_MEM_FRACTION_STATIC}"
        fi
        if [ -n "${SGLANG_TTS_MAX_RUNNING_REQUESTS:-}" ]; then
          printf '      max_running_requests: %s\n' "${SGLANG_TTS_MAX_RUNNING_REQUESTS}"
        fi
        if [ "${SGLANG_TTS_TORCH_COMPILE:-}" = "0" ]; then
          printf '      enable_torch_compile: false\n'
        elif [ "${SGLANG_TTS_TORCH_COMPILE:-}" = "1" ]; then
          printf '      enable_torch_compile: true\n'
        fi
        if [ "${SGLANG_TTS_CUDA_GRAPH:-}" = "0" ]; then
          printf '      disable_cuda_graph: true\n'
        elif [ "${SGLANG_TTS_CUDA_GRAPH:-}" = "1" ]; then
          printf '      disable_cuda_graph: false\n'
        fi
      fi
    } >>"${tmp_config}"
  fi

  mv "${tmp_config}" "${generated_config}"
  export SGLANG_CONFIG="${generated_config}"
}

if [ -z "${SGLANG_CONFIG+x}" ]; then
  export SGLANG_CONFIG=configs/s2pro_tts.yaml
fi
if [ -z "${MODEL_DIR+x}" ]; then
  export MODEL_DIR=/models/s2-pro
fi
if [ -z "${OMP_NUM_THREADS:-}" ]; then
  unset OMP_NUM_THREADS
fi

bash "${SCRIPT_DIR}/ensure_model.sh"
prepare_sglang_config

exec uv run fish-worker
