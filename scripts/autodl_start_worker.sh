#!/usr/bin/env bash
set -euo pipefail

export PATH="${HOME}/.local/bin:${HOME}/.cargo/bin:${PATH}"

AUTODL_FS="${AUTODL_FS:-/root/autodl-fs}"
REPO_DIR="${REPO_DIR:-${AUTODL_FS}/fish-speech}"
REPO_REF="${REPO_REF:-}"
AUTO_SETUP="${AUTO_SETUP:-1}"
WORKER_MANAGE_SGLANG="${WORKER_MANAGE_SGLANG:-1}"
SGLANG_OMNI_DIR="${SGLANG_OMNI_DIR:-${AUTODL_FS}/sglang-omni}"
PYPI_INDEX_URL="${PYPI_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}"
UV_PYTHON_INSTALL_MIRROR="${UV_PYTHON_INSTALL_MIRROR:-https://mirrors.tuna.tsinghua.edu.cn/github-release/astral-sh/python-build-standalone}"

log() {
  printf '[autodl-worker] %s\n' "$*" >&2
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

  export PYTHONUNBUFFERED="${PYTHONUNBUFFERED:-1}"
  log "starting fish-worker for ${MANAGER_URL}"
  exec bash ./scripts/start.sh
}

main "$@"
