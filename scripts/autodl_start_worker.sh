#!/usr/bin/env bash
set -euo pipefail

export PATH="${HOME}/.local/bin:${HOME}/.cargo/bin:${PATH}"

AUTODL_FS="${AUTODL_FS:-/autodl-fs/data}"
REPO_DIR="${REPO_DIR:-/root/src/fish-speech}"
REPO_REF="${REPO_REF:-}"
AUTO_SETUP="${AUTO_SETUP:-1}"
PYPI_INDEX_URL="${PYPI_INDEX_URL:-https://mirrors.aliyun.com/pypi/simple}"
UV_PYTHON_INSTALL_MIRROR="${UV_PYTHON_INSTALL_MIRROR:-https://mirrors.tuna.tsinghua.edu.cn/github-release/astral-sh/python-build-standalone}"
AUTO_UPDATE="${AUTO_UPDATE:-1}"
GIT_PULL_TIMEOUT_SECONDS="${GIT_PULL_TIMEOUT_SECONDS:-120}"
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

restore_env_overrides() {
  if [ -n "${ENV_MANAGER_URL}" ]; then export MANAGER_URL="${ENV_MANAGER_URL}"; fi
  if [ -n "${ENV_WORKER_TOKEN}" ]; then export WORKER_TOKEN="${ENV_WORKER_TOKEN}"; fi
  if [ -n "${ENV_WORKER_ID}" ]; then export WORKER_ID="${ENV_WORKER_ID}"; fi
  if [ -n "${ENV_MODEL_DIR}" ]; then export MODEL_DIR="${ENV_MODEL_DIR}"; fi
  if [ -n "${ENV_CACHE_DIR}" ]; then export CACHE_DIR="${ENV_CACHE_DIR}"; fi
  if [ -n "${ENV_API_SERVER_HOST}" ]; then export API_SERVER_HOST="${ENV_API_SERVER_HOST}"; fi
  if [ -n "${ENV_API_SERVER_PORT}" ]; then export API_SERVER_PORT="${ENV_API_SERVER_PORT}"; fi
  if [ -n "${ENV_API_SERVER_URL}" ]; then export API_SERVER_URL="${ENV_API_SERVER_URL}"; fi
  if [ -n "${ENV_API_SERVER_DECODER_CHECKPOINT_PATH}" ]; then export API_SERVER_DECODER_CHECKPOINT_PATH="${ENV_API_SERVER_DECODER_CHECKPOINT_PATH}"; fi
  if [ -n "${ENV_API_SERVER_DECODER_CONFIG_NAME}" ]; then export API_SERVER_DECODER_CONFIG_NAME="${ENV_API_SERVER_DECODER_CONFIG_NAME}"; fi
  if [ -n "${ENV_API_SERVER_MAX_RUNNING_REQUESTS}" ]; then export API_SERVER_MAX_RUNNING_REQUESTS="${ENV_API_SERVER_MAX_RUNNING_REQUESTS}"; fi
  if [ -n "${ENV_API_SERVER_MAX_QUEUED_REQUESTS}" ]; then export API_SERVER_MAX_QUEUED_REQUESTS="${ENV_API_SERVER_MAX_QUEUED_REQUESTS}"; fi
  if [ -n "${ENV_API_SERVER_TTS_MAX_NEW_TOKENS}" ]; then export API_SERVER_TTS_MAX_NEW_TOKENS="${ENV_API_SERVER_TTS_MAX_NEW_TOKENS}"; fi
  if [ -n "${ENV_API_SERVER_COMPILE}" ]; then export API_SERVER_COMPILE="${ENV_API_SERVER_COMPILE}"; fi
  if [ -n "${ENV_API_SERVER_HALF}" ]; then export API_SERVER_HALF="${ENV_API_SERVER_HALF}"; fi
  if [ -n "${ENV_API_SERVER_WORKERS}" ]; then export API_SERVER_WORKERS="${ENV_API_SERVER_WORKERS}"; fi
  if [ -n "${ENV_API_SERVER_MAX_TEXT_LENGTH}" ]; then export API_SERVER_MAX_TEXT_LENGTH="${ENV_API_SERVER_MAX_TEXT_LENGTH}"; fi
  if [ -n "${ENV_API_SERVER_REFERENCES_DIR}" ]; then export API_SERVER_REFERENCES_DIR="${ENV_API_SERVER_REFERENCES_DIR}"; fi
  if [ -n "${ENV_API_SERVER_EXTRA_ARGS}" ]; then export API_SERVER_EXTRA_ARGS="${ENV_API_SERVER_EXTRA_ARGS}"; fi
  if [ -n "${ENV_API_SERVER_STARTUP_TIMEOUT_SECONDS}" ]; then export API_SERVER_STARTUP_TIMEOUT_SECONDS="${ENV_API_SERVER_STARTUP_TIMEOUT_SECONDS}"; fi
  if [ -n "${ENV_WORKER_MANAGE_API_SERVER}" ]; then export WORKER_MANAGE_API_SERVER="${ENV_WORKER_MANAGE_API_SERVER}"; fi
  if [ -n "${ENV_PYTORCH_ALLOC_CONF}" ]; then export PYTORCH_ALLOC_CONF="${ENV_PYTORCH_ALLOC_CONF}"; fi
  if [ -n "${ENV_HEARTBEAT_INTERVAL_SECONDS}" ]; then export HEARTBEAT_INTERVAL_SECONDS="${ENV_HEARTBEAT_INTERVAL_SECONDS}"; fi
  if [ -n "${ENV_HF_ENDPOINT}" ]; then export HF_ENDPOINT="${ENV_HF_ENDPOINT}"; fi
  if [ -n "${ENV_HFD_SCRIPT}" ]; then export HFD_SCRIPT="${ENV_HFD_SCRIPT}"; fi
  if [ -n "${ENV_HFD_URL}" ]; then export HFD_URL="${ENV_HFD_URL}"; fi
  if [ -n "${ENV_HFD_TOOL}" ]; then export HFD_TOOL="${ENV_HFD_TOOL}"; fi
  if [ -n "${ENV_HFD_THREADS}" ]; then export HFD_THREADS="${ENV_HFD_THREADS}"; fi
  if [ -n "${ENV_MODEL_ID}" ]; then export MODEL_ID="${ENV_MODEL_ID}"; fi
  if [ -n "${ENV_MODEL_REQUIRED_FILES}" ]; then export MODEL_REQUIRED_FILES="${ENV_MODEL_REQUIRED_FILES}"; fi
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

worker_can_run_api_server() {
  local worker_dir="$1"
  (
    cd "${worker_dir}/.."
    "${worker_dir}/.venv/bin/python" - <<'PY' >/dev/null 2>&1
import fish_worker
import tools.api_server
PY
  )
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

  if ! worker_can_run_api_server "${worker_dir}"; then
    log "Fish API server package is missing from worker venv; running AutoDL setup"
    bash "${setup_script}"
  fi
}

pull_repo() {
  if command -v timeout >/dev/null 2>&1; then
    timeout "${GIT_PULL_TIMEOUT_SECONDS}" git "$@"
  else
    git "$@"
  fi
}

# Best-effort code update: never blocks startup. Pulls latest code using the
# same network_turbo acceleration as autodl_setup.sh (enabled for the pull,
# then scoped away in a subshell). Failures/timeouts only log a warning.
git_update() {
  local repo_root="$1"
  local rc=0

  if [ "${AUTO_UPDATE}" != "1" ]; then
    log "AUTO_UPDATE=0; skipping git pull"
    return 0
  fi
  if [ -n "${REPO_REF}" ]; then
    log "REPO_REF is set; skipping git pull (pinned to ${REPO_REF})"
    return 0
  fi
  if [ ! -d "${repo_root}/.git" ]; then
    log "repo root ${repo_root} is not a git checkout; skipping git pull"
    return 0
  fi

  log "updating code from remote (timeout=${GIT_PULL_TIMEOUT_SECONDS}s)"
  if [ -f "${NETWORK_TURBO_SCRIPT}" ]; then
    log "enabling AutoDL network turbo for git pull"
    # source inside a subshell: proxy only applies to this pull, never leaks out
    ( source "${NETWORK_TURBO_SCRIPT}"; pull_repo -C "${repo_root}" pull --ff-only --prune ) || rc=$?
  else
    pull_repo -C "${repo_root}" pull --ff-only --prune || rc=$?
  fi

  if [ "${rc}" -eq 0 ]; then
    log "git pull succeeded"
  else
    log "warning: git pull failed or timed out (rc=${rc}); continuing with existing code"
  fi
  return 0
}

main() {
  local repo_root worker_dir
  repo_root="$(resolve_repo_root)"
  worker_dir="${repo_root}/fish-worker"
  git_update "${repo_root}"
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

  export PYTHONUNBUFFERED="${PYTHONUNBUFFERED:-1}"
  log "starting fish-worker for ${MANAGER_URL}"
  exec bash ./scripts/start.sh
}

main "$@"
