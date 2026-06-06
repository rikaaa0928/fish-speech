#!/usr/bin/env bash
set -euo pipefail

export PATH="${HOME}/.local/bin:${HOME}/.cargo/bin:${PATH}"

AUTODL_FS="${AUTODL_FS:-/autodl-fs/data}"
REPO_DIR="${REPO_DIR:-/root/src/fish-speech}"
REPO_REF="${REPO_REF:-}"
AUTO_SETUP="${AUTO_SETUP:-1}"
ENV_WORKER_MANAGE_SGLANG="${WORKER_MANAGE_SGLANG-}"
WORKER_MANAGE_SGLANG="${WORKER_MANAGE_SGLANG:-1}"
SGLANG_OMNI_DIR="${SGLANG_OMNI_DIR:-/root/src/sglang-omni}"
PYPI_INDEX_URL="${PYPI_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}"
UV_PYTHON_INSTALL_MIRROR="${UV_PYTHON_INSTALL_MIRROR:-https://mirrors.tuna.tsinghua.edu.cn/github-release/astral-sh/python-build-standalone}"

ENV_MANAGER_URL="${MANAGER_URL-}"
ENV_WORKER_TOKEN="${WORKER_TOKEN-}"
ENV_WORKER_ID="${WORKER_ID-}"
ENV_MODEL_DIR="${MODEL_DIR-}"
ENV_CACHE_DIR="${CACHE_DIR-}"
ENV_SGLANG_HOST="${SGLANG_HOST-}"
ENV_SGLANG_PORT="${SGLANG_PORT-}"
ENV_SGLANG_CONFIG="${SGLANG_CONFIG-}"
ENV_SGLANG_MAX_RUNNING_REQUESTS="${SGLANG_MAX_RUNNING_REQUESTS-}"
ENV_SGLANG_MAX_QUEUED_REQUESTS="${SGLANG_MAX_QUEUED_REQUESTS-}"
ENV_SGLANG_EXTRA_ARGS="${SGLANG_EXTRA_ARGS-}"
ENV_SGLANG_TTS_MEM_FRACTION_STATIC="${SGLANG_TTS_MEM_FRACTION_STATIC-}"
ENV_SGLANG_TTS_MAX_RUNNING_REQUESTS="${SGLANG_TTS_MAX_RUNNING_REQUESTS-}"
ENV_SGLANG_TTS_TORCH_COMPILE="${SGLANG_TTS_TORCH_COMPILE-}"
ENV_SGLANG_TTS_CUDA_GRAPH="${SGLANG_TTS_CUDA_GRAPH-}"
ENV_WORKER_MAX_INFLIGHT="${WORKER_MAX_INFLIGHT-}"
ENV_WORKER_MAX_QUEUE="${WORKER_MAX_QUEUE-}"
ENV_HEARTBEAT_INTERVAL_SECONDS="${HEARTBEAT_INTERVAL_SECONDS-}"
ENV_HF_ENDPOINT="${HF_ENDPOINT-}"
ENV_HFD_SCRIPT="${HFD_SCRIPT-}"
ENV_HFD_URL="${HFD_URL-}"
ENV_HFD_TOOL="${HFD_TOOL-}"
ENV_HFD_THREADS="${HFD_THREADS-}"
ENV_MODEL_ID="${MODEL_ID-}"
ENV_MODEL_REQUIRED_FILES="${MODEL_REQUIRED_FILES-}"

log() {
  printf '[autodl-worker] %s\n' "$*" >&2
}

prepare_sglang_config() {
  local worker_dir="$1"

  if [ -z "${SGLANG_CONFIG:-}" ] || [ ! -f "${SGLANG_CONFIG}" ]; then
    return 0
  fi

  local generated_dir="${worker_dir}/.generated"
  local generated_config="${generated_dir}/$(basename "${SGLANG_CONFIG}")"
  local replaced_model_path=0
  mkdir -p "${generated_dir}"
  while IFS= read -r line; do
    case "${line}" in
      model_path:*)
        if [ -n "${MODEL_DIR:-}" ]; then
          printf 'model_path: %s\n' "${MODEL_DIR}"
          replaced_model_path=1
        else
          printf '%s\n' "${line}"
        fi
        ;;
      *) printf '%s\n' "${line}" ;;
    esac
  done <"${SGLANG_CONFIG}" >"${generated_config}"

  if [ "${replaced_model_path}" = "0" ] && [ -n "${MODEL_DIR:-}" ]; then
    printf 'model_path: %s\n' "${MODEL_DIR}" >>"${generated_config}"
  fi

  if [ -n "${SGLANG_TTS_MEM_FRACTION_STATIC:-}" ] || [ -n "${SGLANG_TTS_MAX_RUNNING_REQUESTS:-}" ] || [ -n "${SGLANG_TTS_TORCH_COMPILE:-}" ] || [ -n "${SGLANG_TTS_CUDA_GRAPH:-}" ]; then
    {
      printf 'runtime_overrides:\n'
      printf '  tts_engine:\n'
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
    } >>"${generated_config}"
  fi
  export SGLANG_CONFIG="${generated_config}"
}

restore_env_overrides() {
  if [ -n "${ENV_MANAGER_URL}" ]; then export MANAGER_URL="${ENV_MANAGER_URL}"; fi
  if [ -n "${ENV_WORKER_TOKEN}" ]; then export WORKER_TOKEN="${ENV_WORKER_TOKEN}"; fi
  if [ -n "${ENV_WORKER_ID}" ]; then export WORKER_ID="${ENV_WORKER_ID}"; fi
  if [ -n "${ENV_MODEL_DIR}" ]; then export MODEL_DIR="${ENV_MODEL_DIR}"; fi
  if [ -n "${ENV_CACHE_DIR}" ]; then export CACHE_DIR="${ENV_CACHE_DIR}"; fi
  if [ -n "${ENV_SGLANG_HOST}" ]; then export SGLANG_HOST="${ENV_SGLANG_HOST}"; fi
  if [ -n "${ENV_SGLANG_PORT}" ]; then export SGLANG_PORT="${ENV_SGLANG_PORT}"; fi
  if [ -n "${ENV_SGLANG_CONFIG}" ]; then export SGLANG_CONFIG="${ENV_SGLANG_CONFIG}"; fi
  if [ -n "${ENV_SGLANG_MAX_RUNNING_REQUESTS}" ]; then export SGLANG_MAX_RUNNING_REQUESTS="${ENV_SGLANG_MAX_RUNNING_REQUESTS}"; fi
  if [ -n "${ENV_SGLANG_MAX_QUEUED_REQUESTS}" ]; then export SGLANG_MAX_QUEUED_REQUESTS="${ENV_SGLANG_MAX_QUEUED_REQUESTS}"; fi
  if [ -n "${ENV_SGLANG_EXTRA_ARGS}" ]; then export SGLANG_EXTRA_ARGS="${ENV_SGLANG_EXTRA_ARGS}"; fi
  if [ -n "${ENV_SGLANG_TTS_MEM_FRACTION_STATIC}" ]; then export SGLANG_TTS_MEM_FRACTION_STATIC="${ENV_SGLANG_TTS_MEM_FRACTION_STATIC}"; fi
  if [ -n "${ENV_SGLANG_TTS_MAX_RUNNING_REQUESTS}" ]; then export SGLANG_TTS_MAX_RUNNING_REQUESTS="${ENV_SGLANG_TTS_MAX_RUNNING_REQUESTS}"; fi
  if [ -n "${ENV_SGLANG_TTS_TORCH_COMPILE}" ]; then export SGLANG_TTS_TORCH_COMPILE="${ENV_SGLANG_TTS_TORCH_COMPILE}"; fi
  if [ -n "${ENV_SGLANG_TTS_CUDA_GRAPH}" ]; then export SGLANG_TTS_CUDA_GRAPH="${ENV_SGLANG_TTS_CUDA_GRAPH}"; fi
  if [ -n "${ENV_WORKER_MAX_INFLIGHT}" ]; then export WORKER_MAX_INFLIGHT="${ENV_WORKER_MAX_INFLIGHT}"; fi
  if [ -n "${ENV_WORKER_MAX_QUEUE}" ]; then export WORKER_MAX_QUEUE="${ENV_WORKER_MAX_QUEUE}"; fi
  if [ -n "${ENV_WORKER_MANAGE_SGLANG}" ]; then export WORKER_MANAGE_SGLANG="${ENV_WORKER_MANAGE_SGLANG}"; fi
  if [ -n "${ENV_HEARTBEAT_INTERVAL_SECONDS}" ]; then export HEARTBEAT_INTERVAL_SECONDS="${ENV_HEARTBEAT_INTERVAL_SECONDS}"; fi
  if [ -n "${ENV_HF_ENDPOINT}" ]; then export HF_ENDPOINT="${ENV_HF_ENDPOINT}"; fi
  if [ -n "${ENV_HFD_SCRIPT}" ]; then export HFD_SCRIPT="${ENV_HFD_SCRIPT}"; fi
  if [ -n "${ENV_HFD_URL}" ]; then export HFD_URL="${ENV_HFD_URL}"; fi
  if [ -n "${ENV_HFD_TOOL}" ]; then export HFD_TOOL="${ENV_HFD_TOOL}"; fi
  if [ -n "${ENV_HFD_THREADS}" ]; then export HFD_THREADS="${ENV_HFD_THREADS}"; fi
  if [ -n "${ENV_MODEL_ID}" ]; then export MODEL_ID="${ENV_MODEL_ID}"; fi
  if [ -n "${ENV_MODEL_REQUIRED_FILES}" ]; then export MODEL_REQUIRED_FILES="${ENV_MODEL_REQUIRED_FILES}"; fi
}

export PIP_INDEX_URL="${PIP_INDEX_URL:-${PYPI_INDEX_URL}}"
export UV_INDEX_URL="${UV_INDEX_URL:-${PYPI_INDEX_URL}}"
export UV_DEFAULT_INDEX="${UV_DEFAULT_INDEX:-${PYPI_INDEX_URL}}"
export UV_PYTHON_INSTALL_MIRROR

resolve_repo_root() {
  local script_dir
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

  if [ -d "${script_dir}/../fish-worker" ] && [ -d "${script_dir}/../fish-manager" ]; then
    cd "${script_dir}/.." && pwd
    return 0
  fi

  if [ -d "${PWD}/fish-worker" ] && [ -d "${PWD}/fish-manager" ]; then
    pwd
    return 0
  fi

  if [ -z "${REPO_URL:-}" ]; then
    printf 'Cannot find fish-worker/fish-manager. Set REPO_URL to clone the project first.\n' >&2
    exit 1
  fi

  mkdir -p "$(dirname "${REPO_DIR}")"
  if [ ! -d "${REPO_DIR}/.git" ]; then
    log "cloning ${REPO_URL} to ${REPO_DIR}"
    git clone "${REPO_URL}" "${REPO_DIR}"
  fi
  if [ -n "${REPO_REF}" ]; then
    log "checking out ${REPO_REF}"
    git -C "${REPO_DIR}" fetch origin "${REPO_REF}" || true
    git -C "${REPO_DIR}" checkout "${REPO_REF}"
  fi
  cd "${REPO_DIR}" && pwd
}

ensure_setup() {
  local repo_root="$1"
  local worker_dir="${repo_root}/fish-worker"
  local setup_script="${repo_root}/scripts/autodl_setup.sh"

  if [ "${AUTO_SETUP}" != "1" ]; then
    return 0
  fi

  if [ ! -x "${worker_dir}/.venv/bin/fish-worker" ]; then
    log "worker venv is missing; running AutoDL setup"
    bash "${setup_script}"
    return 0
  fi

  if [ ! -f "${worker_dir}/.env" ]; then
    log "worker .env is missing; running AutoDL setup"
    bash "${setup_script}"
    return 0
  fi

  set -a
  # shellcheck disable=SC1090
  source "${worker_dir}/.env"
  set +a
  restore_env_overrides

  if [ "${WORKER_MANAGE_SGLANG}" != "0" ] && [ ! -x "${worker_dir}/.venv/bin/sgl-omni" ]; then
    log "sgl-omni is missing; running AutoDL setup"
    bash "${setup_script}"
  fi
}

main() {
  local repo_root worker_dir
  repo_root="$(resolve_repo_root)"
  worker_dir="${repo_root}/fish-worker"
  ensure_setup "${repo_root}"

  cd "${worker_dir}"
  if [ ! -f .env ]; then
    printf 'Missing %s/.env. Run scripts/autodl_setup.sh first or set AUTO_SETUP=1.\n' "${worker_dir}" >&2
    exit 1
  fi

  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
  restore_env_overrides

  if [ -z "${MANAGER_URL:-}" ]; then
    printf 'MANAGER_URL is required in %s/.env.\n' "${worker_dir}" >&2
    exit 1
  fi
  if [ "${MANAGER_URL}" = "wss://manager.example.com/internal/workers/ws" ]; then
    printf 'MANAGER_URL still uses the placeholder value in %s/.env.\n' "${worker_dir}" >&2
    exit 1
  fi
  if [ -z "${WORKER_TOKEN:-}" ]; then
    printf 'WORKER_TOKEN is required in %s/.env.\n' "${worker_dir}" >&2
    exit 1
  fi
  if [ "${WORKER_TOKEN}" = "replace-me" ]; then
    printf 'WORKER_TOKEN still uses the placeholder value in %s/.env.\n' "${worker_dir}" >&2
    exit 1
  fi

  if [ -z "${SGLANG_CONFIG:-}" ] && [ -f "${SGLANG_OMNI_DIR}/examples/configs/s2pro_tts.yaml" ]; then
    export SGLANG_CONFIG="${SGLANG_OMNI_DIR}/examples/configs/s2pro_tts.yaml"
  fi
  if [ -n "${SGLANG_CONFIG:-}" ] && [ ! -f "${SGLANG_CONFIG}" ]; then
    log "warning: SGLANG_CONFIG does not exist: ${SGLANG_CONFIG}; starting without --config"
    export SGLANG_CONFIG=""
  fi
  prepare_sglang_config "${worker_dir}"

  export PYTHONUNBUFFERED="${PYTHONUNBUFFERED:-1}"
  log "starting fish-worker for ${MANAGER_URL}"
  exec bash ./scripts/start.sh
}

main "$@"
