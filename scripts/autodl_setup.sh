#!/usr/bin/env bash
set -euo pipefail

export PATH="${HOME}/.local/bin:${PATH}"

AUTODL_FS="${AUTODL_FS:-/autodl-fs/data}"
AUTODL_TMP="${AUTODL_TMP:-/root/autodl-tmp}"
REPO_DIR="${REPO_DIR:-/root/src/fish-speech}"
REPO_REF="${REPO_REF:-}"
PYTHON_BIN="${PYTHON_BIN:-}"
PYTHON_VERSION="${PYTHON_VERSION:-3.11}"
PYPI_INDEX_URL="${PYPI_INDEX_URL:-https://mirrors.aliyun.com/pypi/simple}"
UV_PYTHON_INSTALL_MIRROR="${UV_PYTHON_INSTALL_MIRROR:-https://mirrors.tuna.tsinghua.edu.cn/github-release/astral-sh/python-build-standalone}"
UV_EXTRA="${UV_EXTRA:-cu128}"
DOWNLOAD_MODEL="${DOWNLOAD_MODEL:-1}"
OVERWRITE_ENV="${OVERWRITE_ENV:-0}"
HFD_SCRIPT="${HFD_SCRIPT:-${AUTODL_TMP}/cache/hfd.sh}"
HFD_URL="${HFD_URL:-https://hf-mirror.com/hfd/hfd.sh}"
HFD_TOOL="${HFD_TOOL:-aria2c}"
HFD_THREADS="${HFD_THREADS:-8}"
GIT_RETRY_ATTEMPTS="${GIT_RETRY_ATTEMPTS:-3}"
GITHUB_ACCELERATION="${GITHUB_ACCELERATION:-auto}"
NETWORK_TURBO_SCRIPT="${NETWORK_TURBO_SCRIPT:-/etc/network_turbo}"

ENV_MANAGER_URL="${MANAGER_URL-}"
ENV_WORKER_TOKEN="${WORKER_TOKEN-}"
ENV_WORKER_ID="${WORKER_ID-}"
ENV_MODEL_DIR="${MODEL_DIR-}"
ENV_CACHE_DIR="${CACHE_DIR-}"
ENV_API_SERVER_HOST="${API_SERVER_HOST-}"
ENV_API_SERVER_PORT="${API_SERVER_PORT-}"
ENV_API_SERVER_URL="${API_SERVER_URL-}"
ENV_API_SERVER_DECODER_CHECKPOINT_PATH="${API_SERVER_DECODER_CHECKPOINT_PATH-}"
ENV_API_SERVER_DECODER_CONFIG_NAME="${API_SERVER_DECODER_CONFIG_NAME-}"
ENV_API_SERVER_MAX_RUNNING_REQUESTS="${API_SERVER_MAX_RUNNING_REQUESTS-}"
ENV_API_SERVER_MAX_QUEUED_REQUESTS="${API_SERVER_MAX_QUEUED_REQUESTS-}"
ENV_API_SERVER_TTS_MAX_NEW_TOKENS="${API_SERVER_TTS_MAX_NEW_TOKENS-}"
ENV_API_SERVER_COMPILE="${API_SERVER_COMPILE-}"
ENV_API_SERVER_HALF="${API_SERVER_HALF-}"
ENV_API_SERVER_WORKERS="${API_SERVER_WORKERS-}"
ENV_API_SERVER_MAX_TEXT_LENGTH="${API_SERVER_MAX_TEXT_LENGTH-}"
ENV_API_SERVER_REFERENCES_DIR="${API_SERVER_REFERENCES_DIR-}"
ENV_API_SERVER_EXTRA_ARGS="${API_SERVER_EXTRA_ARGS-}"
ENV_API_SERVER_STARTUP_TIMEOUT_SECONDS="${API_SERVER_STARTUP_TIMEOUT_SECONDS-}"
ENV_WORKER_MANAGE_API_SERVER="${WORKER_MANAGE_API_SERVER-}"
ENV_PYTORCH_ALLOC_CONF="${PYTORCH_ALLOC_CONF-}"
ENV_HEARTBEAT_INTERVAL_SECONDS="${HEARTBEAT_INTERVAL_SECONDS-}"

log() {
  printf '[autodl-setup] %s\n' "$*" >&2
}

if [ -n "${PYPI_INDEX_URL}" ]; then
  export PIP_INDEX_URL="${PIP_INDEX_URL:-${PYPI_INDEX_URL}}"
  export UV_INDEX_URL="${UV_INDEX_URL:-${PYPI_INDEX_URL}}"
  export UV_DEFAULT_INDEX="${UV_DEFAULT_INDEX:-${PYPI_INDEX_URL}}"
elif [ -n "${PIP_INDEX_URL:-}" ]; then
  export UV_INDEX_URL="${UV_INDEX_URL:-${PIP_INDEX_URL}}"
  export UV_DEFAULT_INDEX="${UV_DEFAULT_INDEX:-${PIP_INDEX_URL}}"
fi
export UV_PYTHON_INSTALL_MIRROR

run_root() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  else
    log "skip root command because sudo is unavailable: $*"
    return 1
  fi
}

github_accelerated_git() {
  if [ "${GITHUB_ACCELERATION}" = "0" ]; then
    git "$@"
    return 0
  fi

  if [ ! -f "${NETWORK_TURBO_SCRIPT}" ]; then
    if [ "${GITHUB_ACCELERATION}" = "1" ]; then
      log "warning: ${NETWORK_TURBO_SCRIPT} not found; running git without acceleration"
    fi
    git "$@"
    return 0
  fi

  log "enabling AutoDL network turbo for git"
  (
    set +u
    # shellcheck disable=SC1090
    source "${NETWORK_TURBO_SCRIPT}"
    set -u
    git "$@"
    unset http_proxy
    unset https_proxy
  )
}

install_system_packages() {
  if ! command -v apt-get >/dev/null 2>&1; then
    log "apt-get not found; skip system package installation"
    return 0
  fi

  log "installing system packages"
  run_root apt-get update
  run_root apt-get install -y --no-install-recommends \
    aria2 \
    build-essential \
    ca-certificates \
    cmake \
    curl \
    ffmpeg \
    git \
    git-lfs \
    libasound-dev \
    libportaudio2 \
    libportaudiocpp0 \
    libsndfile1 \
    libsox-dev \
    libssl-dev \
    pkg-config \
    portaudio19-dev \
    python3-dev \
    python3-pip \
    python3-venv
  git lfs install --skip-repo || true
}

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
    github_accelerated_git clone "${REPO_URL}" "${REPO_DIR}"
  fi
  if [ -n "${REPO_REF}" ]; then
    log "checking out ${REPO_REF}"
    github_accelerated_git -C "${REPO_DIR}" fetch origin "${REPO_REF}" || true
    git -C "${REPO_DIR}" checkout "${REPO_REF}"
  fi
  cd "${REPO_DIR}" && pwd
}

ensure_uv() {
  if command -v uv >/dev/null 2>&1; then
    return 0
  fi

  local pip_index_args=()
  if [ -n "${PYPI_INDEX_URL}" ]; then
    pip_index_args=(-i "${PYPI_INDEX_URL}")
    log "installing uv from ${PYPI_INDEX_URL}"
  else
    log "installing uv"
  fi
  if "${PYTHON_BIN}" -m pip --version >/dev/null 2>&1; then
    "${PYTHON_BIN}" -m pip install --user --upgrade "${pip_index_args[@]}" uv
  elif python3 -m pip --version >/dev/null 2>&1; then
    python3 -m pip install --user --upgrade "${pip_index_args[@]}" uv
  else
    printf 'pip is required to install uv.\n' >&2
    exit 1
  fi
  export PATH="${HOME}/.local/bin:${PATH}"
}

select_python_bin() {
  if [ -n "${PYTHON_BIN}" ]; then
    return 0
  fi
  if [ -x /root/miniconda3/bin/python ]; then
    PYTHON_BIN=/root/miniconda3/bin/python
  else
    PYTHON_BIN=python3
  fi
}

ensure_python_bin() {
  if "${PYTHON_BIN}" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY
  then
    return 0
  fi

  log "installing Python ${PYTHON_VERSION} via uv"
  uv python install "${PYTHON_VERSION}"
  PYTHON_BIN="${PYTHON_VERSION}"
}

load_existing_envs() {
  local worker_env="${1}/fish-worker/.env"

  if [ -f "${worker_env}" ]; then
    set -a
    # shellcheck disable=SC1090
    source "${worker_env}"
    set +a
  fi

  if [ -n "${ENV_MANAGER_URL}" ]; then MANAGER_URL="${ENV_MANAGER_URL}"; fi
  if [ -n "${ENV_WORKER_TOKEN}" ]; then WORKER_TOKEN="${ENV_WORKER_TOKEN}"; fi
  if [ -n "${ENV_WORKER_ID}" ]; then WORKER_ID="${ENV_WORKER_ID}"; fi
  if [ -n "${ENV_MODEL_DIR}" ]; then MODEL_DIR="${ENV_MODEL_DIR}"; fi
  if [ -n "${ENV_CACHE_DIR}" ]; then CACHE_DIR="${ENV_CACHE_DIR}"; fi
  if [ -n "${ENV_API_SERVER_HOST}" ]; then API_SERVER_HOST="${ENV_API_SERVER_HOST}"; fi
  if [ -n "${ENV_API_SERVER_PORT}" ]; then API_SERVER_PORT="${ENV_API_SERVER_PORT}"; fi
  if [ -n "${ENV_API_SERVER_URL}" ]; then API_SERVER_URL="${ENV_API_SERVER_URL}"; fi
  if [ -n "${ENV_API_SERVER_DECODER_CHECKPOINT_PATH}" ]; then API_SERVER_DECODER_CHECKPOINT_PATH="${ENV_API_SERVER_DECODER_CHECKPOINT_PATH}"; fi
  if [ -n "${ENV_API_SERVER_DECODER_CONFIG_NAME}" ]; then API_SERVER_DECODER_CONFIG_NAME="${ENV_API_SERVER_DECODER_CONFIG_NAME}"; fi
  if [ -n "${ENV_API_SERVER_MAX_RUNNING_REQUESTS}" ]; then API_SERVER_MAX_RUNNING_REQUESTS="${ENV_API_SERVER_MAX_RUNNING_REQUESTS}"; fi
  if [ -n "${ENV_API_SERVER_MAX_QUEUED_REQUESTS}" ]; then API_SERVER_MAX_QUEUED_REQUESTS="${ENV_API_SERVER_MAX_QUEUED_REQUESTS}"; fi
  if [ -n "${ENV_API_SERVER_TTS_MAX_NEW_TOKENS}" ]; then API_SERVER_TTS_MAX_NEW_TOKENS="${ENV_API_SERVER_TTS_MAX_NEW_TOKENS}"; fi
  if [ -n "${ENV_API_SERVER_COMPILE}" ]; then API_SERVER_COMPILE="${ENV_API_SERVER_COMPILE}"; fi
  if [ -n "${ENV_API_SERVER_HALF}" ]; then API_SERVER_HALF="${ENV_API_SERVER_HALF}"; fi
  if [ -n "${ENV_API_SERVER_WORKERS}" ]; then API_SERVER_WORKERS="${ENV_API_SERVER_WORKERS}"; fi
  if [ -n "${ENV_API_SERVER_MAX_TEXT_LENGTH}" ]; then API_SERVER_MAX_TEXT_LENGTH="${ENV_API_SERVER_MAX_TEXT_LENGTH}"; fi
  if [ -n "${ENV_API_SERVER_REFERENCES_DIR}" ]; then API_SERVER_REFERENCES_DIR="${ENV_API_SERVER_REFERENCES_DIR}"; fi
  if [ -n "${ENV_API_SERVER_EXTRA_ARGS}" ]; then API_SERVER_EXTRA_ARGS="${ENV_API_SERVER_EXTRA_ARGS}"; fi
  if [ -n "${ENV_API_SERVER_STARTUP_TIMEOUT_SECONDS}" ]; then API_SERVER_STARTUP_TIMEOUT_SECONDS="${ENV_API_SERVER_STARTUP_TIMEOUT_SECONDS}"; fi
  if [ -n "${ENV_WORKER_MANAGE_API_SERVER}" ]; then WORKER_MANAGE_API_SERVER="${ENV_WORKER_MANAGE_API_SERVER}"; fi
  if [ -n "${ENV_PYTORCH_ALLOC_CONF}" ]; then PYTORCH_ALLOC_CONF="${ENV_PYTORCH_ALLOC_CONF}"; fi
  if [ -n "${ENV_HEARTBEAT_INTERVAL_SECONDS}" ]; then HEARTBEAT_INTERVAL_SECONDS="${ENV_HEARTBEAT_INTERVAL_SECONDS}"; fi
}

write_worker_env() {
  local repo_root="$1"
  local env_file="${repo_root}/fish-worker/.env"

  if [ -f "${env_file}" ] && [ "${OVERWRITE_ENV}" != "1" ]; then
    log "keeping existing ${env_file}"
    return 0
  fi

  WORKER_TOKEN="${WORKER_TOKEN:-replace-me}"
  MANAGER_URL="${MANAGER_URL:-wss://manager.example.com/internal/workers/ws}"
  MODEL_ID="${MODEL_ID:-fishaudio/s2-pro}"
  MODEL_DIR="${MODEL_DIR:-${AUTODL_FS}/models/s2-pro}"
  CACHE_DIR="${CACHE_DIR:-${AUTODL_TMP}/cache}"
  HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"
  MODEL_REQUIRED_FILES="${MODEL_REQUIRED_FILES:-codec.pth model-00001-of-00002.safetensors model-00002-of-00002.safetensors}"
  API_SERVER_HOST="${API_SERVER_HOST:-127.0.0.1}"
  API_SERVER_PORT="${API_SERVER_PORT:-8000}"
  API_SERVER_URL="${API_SERVER_URL:-}"
  API_SERVER_DECODER_CHECKPOINT_PATH="${API_SERVER_DECODER_CHECKPOINT_PATH:-${MODEL_DIR}/codec.pth}"
  API_SERVER_DECODER_CONFIG_NAME="${API_SERVER_DECODER_CONFIG_NAME:-modded_dac_vq}"
  API_SERVER_MAX_RUNNING_REQUESTS="${API_SERVER_MAX_RUNNING_REQUESTS:-1}"
  API_SERVER_MAX_QUEUED_REQUESTS="${API_SERVER_MAX_QUEUED_REQUESTS:-0}"
  API_SERVER_TTS_MAX_NEW_TOKENS="${API_SERVER_TTS_MAX_NEW_TOKENS:-1024}"
  API_SERVER_COMPILE="${API_SERVER_COMPILE:-1}"
  API_SERVER_HALF="${API_SERVER_HALF:-0}"
  API_SERVER_WORKERS="${API_SERVER_WORKERS:-1}"
  API_SERVER_MAX_TEXT_LENGTH="${API_SERVER_MAX_TEXT_LENGTH:-0}"
  API_SERVER_REFERENCES_DIR="${API_SERVER_REFERENCES_DIR:-references}"
  API_SERVER_EXTRA_ARGS="${API_SERVER_EXTRA_ARGS:-}"
  PYTORCH_ALLOC_CONF="${PYTORCH_ALLOC_CONF:-expandable_segments:True}"
  API_SERVER_STARTUP_TIMEOUT_SECONDS="${API_SERVER_STARTUP_TIMEOUT_SECONDS:-900}"
  WORKER_MANAGE_API_SERVER="${WORKER_MANAGE_API_SERVER:-1}"
  HEARTBEAT_INTERVAL_SECONDS="${HEARTBEAT_INTERVAL_SECONDS:-5}"

  mkdir -p "${MODEL_DIR}" "${CACHE_DIR}" "${API_SERVER_REFERENCES_DIR}"
  umask 077
  {
    printf 'MANAGER_URL=%s\n' "${MANAGER_URL}"
    printf 'WORKER_TOKEN=%s\n' "${WORKER_TOKEN}"
    printf 'WORKER_ID=%s\n' "${WORKER_ID:-}"
    printf 'HF_ENDPOINT=%s\n' "${HF_ENDPOINT}"
    printf 'HFD_SCRIPT=%s\n' "${HFD_SCRIPT}"
    printf 'HFD_URL=%s\n' "${HFD_URL}"
    printf 'HFD_TOOL=%s\n' "${HFD_TOOL}"
    printf 'HFD_THREADS=%s\n' "${HFD_THREADS}"
    printf 'MODEL_ID=%s\n' "${MODEL_ID}"
    printf 'MODEL_DIR=%s\n' "${MODEL_DIR}"
    printf 'MODEL_REQUIRED_FILES=%q\n' "${MODEL_REQUIRED_FILES}"
    printf 'CACHE_DIR=%s\n' "${CACHE_DIR}"
    printf 'API_SERVER_HOST=%s\n' "${API_SERVER_HOST}"
    printf 'API_SERVER_PORT=%s\n' "${API_SERVER_PORT}"
    printf 'API_SERVER_URL=%s\n' "${API_SERVER_URL}"
    printf 'API_SERVER_DECODER_CHECKPOINT_PATH=%s\n' "${API_SERVER_DECODER_CHECKPOINT_PATH}"
    printf 'API_SERVER_DECODER_CONFIG_NAME=%s\n' "${API_SERVER_DECODER_CONFIG_NAME}"
    printf 'API_SERVER_MAX_RUNNING_REQUESTS=%s\n' "${API_SERVER_MAX_RUNNING_REQUESTS}"
    printf 'API_SERVER_MAX_QUEUED_REQUESTS=%s\n' "${API_SERVER_MAX_QUEUED_REQUESTS}"
    printf 'API_SERVER_TTS_MAX_NEW_TOKENS=%s\n' "${API_SERVER_TTS_MAX_NEW_TOKENS}"
    printf 'API_SERVER_COMPILE=%s\n' "${API_SERVER_COMPILE}"
    printf 'API_SERVER_HALF=%s\n' "${API_SERVER_HALF}"
    printf 'API_SERVER_WORKERS=%s\n' "${API_SERVER_WORKERS}"
    printf 'API_SERVER_MAX_TEXT_LENGTH=%s\n' "${API_SERVER_MAX_TEXT_LENGTH}"
    printf 'API_SERVER_REFERENCES_DIR=%s\n' "${API_SERVER_REFERENCES_DIR}"
    printf 'API_SERVER_EXTRA_ARGS=%q\n' "${API_SERVER_EXTRA_ARGS}"
    printf 'PYTORCH_ALLOC_CONF=%s\n' "${PYTORCH_ALLOC_CONF}"
    printf 'API_SERVER_STARTUP_TIMEOUT_SECONDS=%s\n' "${API_SERVER_STARTUP_TIMEOUT_SECONDS}"
    printf 'WORKER_MANAGE_API_SERVER=%s\n' "${WORKER_MANAGE_API_SERVER}"
    printf 'HEARTBEAT_INTERVAL_SECONDS=%s\n' "${HEARTBEAT_INTERVAL_SECONDS}"
  } >"${env_file}"
  log "wrote ${env_file}"
}

setup_worker_venv() {
  local repo_root="$1"
  local worker_dir="${repo_root}/fish-worker"
  log "setting up worker Python environment with Fish API server dependencies"
  cd "${repo_root}"
  UV_PROJECT_ENVIRONMENT="${worker_dir}/.venv" uv sync --extra "${UV_EXTRA}" --no-dev --inexact -p "${PYTHON_BIN}"
  uv pip install --python "${worker_dir}/.venv/bin/python" -e "${worker_dir}"
}

download_model() {
  local worker_dir="$1"

  if [ "${DOWNLOAD_MODEL}" = "0" ]; then
    log "DOWNLOAD_MODEL=0; skip model download"
    return 0
  fi

  log "ensuring model files are present"
  cd "${worker_dir}"
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
  bash ./scripts/ensure_model.sh
}

main() {
  install_system_packages
  select_python_bin
  local repo_root
  repo_root="$(resolve_repo_root)"
  log "repo root: ${repo_root}"

  ensure_uv
  ensure_python_bin
  load_existing_envs "${repo_root}"
  setup_worker_venv "${repo_root}"
  write_worker_env "${repo_root}"
  download_model "${repo_root}/fish-worker"

  log "done"
  log "start worker:  bash ${repo_root}/scripts/autodl_start_worker.sh"
}

main "$@"
