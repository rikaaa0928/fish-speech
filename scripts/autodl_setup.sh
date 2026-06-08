#!/usr/bin/env bash
set -euo pipefail

export PATH="${HOME}/.local/bin:${PATH}"

AUTODL_FS="${AUTODL_FS:-/autodl-fs/data}"
REPO_DIR="${REPO_DIR:-/root/src/fish-speech}"
REPO_REF="${REPO_REF:-}"
PYTHON_BIN="${PYTHON_BIN:-}"
PYTHON_VERSION="${PYTHON_VERSION:-3.11}"
PYPI_INDEX_URL="${PYPI_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}"
PYTORCH_INDEX_URL="${PYTORCH_INDEX_URL:-https://mirrors.aliyun.com/pytorch-wheels/cu128}"
UV_PYTHON_INSTALL_MIRROR="${UV_PYTHON_INSTALL_MIRROR:-https://mirrors.tuna.tsinghua.edu.cn/github-release/astral-sh/python-build-standalone}"
TORCH_PACKAGES="${TORCH_PACKAGES:-torch==2.8.0 torchvision==0.23.0 torchaudio==2.8.0}"
INSTALL_TORCH="${INSTALL_TORCH:-auto}"
INSTALL_SGLANG_OMNI="${INSTALL_SGLANG_OMNI:-1}"
INSTALL_SGLANG="${INSTALL_SGLANG:-${INSTALL_SGLANG_OMNI}}"
SGLANG_INSTALL_SPEC="${SGLANG_INSTALL_SPEC:-}"
INSTALL_FLASHINFER="${INSTALL_FLASHINFER:-0}"
FLASHINFER_INDEX_URL="${FLASHINFER_INDEX_URL:-https://flashinfer.ai/whl/cu128/torch2.8/}"
DOWNLOAD_MODEL="${DOWNLOAD_MODEL:-1}"
OVERWRITE_ENV="${OVERWRITE_ENV:-0}"
SGLANG_OMNI_REPO="${SGLANG_OMNI_REPO:-https://github.com/sgl-project/sglang-omni.git}"
SGLANG_OMNI_REF="${SGLANG_OMNI_REF:-main}"
SGLANG_OMNI_DIR="${SGLANG_OMNI_DIR:-/root/src/sglang-omni}"
SGLANG_OMNI_UV_OVERRIDES="${SGLANG_OMNI_UV_OVERRIDES:-protobuf>=6.31.1,<7.0.0}"
SGLANG_OMNI_UV_OVERRIDES_FILE="${SGLANG_OMNI_UV_OVERRIDES_FILE:-}"
HFD_SCRIPT="${HFD_SCRIPT:-${AUTODL_FS}/hfd.sh}"
HFD_URL="${HFD_URL:-https://hf-mirror.com/hfd/hfd.sh}"
HFD_TOOL="${HFD_TOOL:-aria2c}"
HFD_THREADS="${HFD_THREADS:-8}"
GIT_RETRY_ATTEMPTS="${GIT_RETRY_ATTEMPTS:-3}"
GITHUB_ACCELERATION="${GITHUB_ACCELERATION:-auto}"
NETWORK_TURBO_SCRIPT="${NETWORK_TURBO_SCRIPT:-/etc/network_turbo}"

ENV_MANAGER_URL="${MANAGER_URL-}"
ENV_WORKER_TOKEN="${WORKER_TOKEN-}"
ENV_MODEL_DIR="${MODEL_DIR-}"
ENV_CACHE_DIR="${CACHE_DIR-}"

log() {
  printf '[autodl-setup] %s\n' "$*" >&2
}

export PIP_INDEX_URL="${PIP_INDEX_URL:-${PYPI_INDEX_URL}}"
export UV_INDEX_URL="${UV_INDEX_URL:-${PYPI_INDEX_URL}}"
export UV_DEFAULT_INDEX="${UV_DEFAULT_INDEX:-${PYPI_INDEX_URL}}"
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
    build-essential \
    ca-certificates \
    curl \
    aria2 \
    ffmpeg \
    git \
    git-lfs \
    libsndfile1 \
    libssl-dev \
    pkg-config \
    python3-pip
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

  log "installing uv from ${PYPI_INDEX_URL}"
  if "${PYTHON_BIN}" -m pip --version >/dev/null 2>&1; then
    "${PYTHON_BIN}" -m pip install --user --upgrade -i "${PYPI_INDEX_URL}" uv
  elif python3 -m pip --version >/dev/null 2>&1; then
    python3 -m pip install --user --upgrade -i "${PYPI_INDEX_URL}" uv
  else
    printf 'pip is required to install uv from the configured domestic PyPI mirror.\n' >&2
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

generate_secret() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 24
  else
    "${PYTHON_BIN}" - <<'PY'
import secrets
print(secrets.token_hex(24))
PY
  fi
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
  if [ -n "${ENV_MODEL_DIR}" ]; then MODEL_DIR="${ENV_MODEL_DIR}"; fi
  if [ -n "${ENV_CACHE_DIR}" ]; then CACHE_DIR="${ENV_CACHE_DIR}"; fi
}

write_worker_env() {
  local repo_root="$1"
  local env_file="${repo_root}/fish-worker/.env"
  local default_config="${SGLANG_OMNI_DIR}/examples/configs/s2pro_tts.yaml"

  if [ -f "${env_file}" ] && [ "${OVERWRITE_ENV}" != "1" ]; then
    log "keeping existing ${env_file}"
    return 0
  fi

  WORKER_TOKEN="${WORKER_TOKEN:-replace-me}"
  MANAGER_URL="${MANAGER_URL:-wss://manager.example.com/internal/workers/ws}"
  MODEL_ID="${MODEL_ID:-fishaudio/s2-pro}"
  MODEL_DIR="${MODEL_DIR:-/autodl-fs/data/models/s2-pro}"
  CACHE_DIR="${CACHE_DIR:-${AUTODL_FS}/cache}"
  HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"
  MODEL_REQUIRED_FILES="${MODEL_REQUIRED_FILES:-codec.pth model-00001-of-00002.safetensors model-00002-of-00002.safetensors}"
  SGLANG_HOST="${SGLANG_HOST:-127.0.0.1}"
  SGLANG_PORT="${SGLANG_PORT:-8000}"
  SGLANG_CONFIG="${SGLANG_CONFIG:-${default_config}}"
  SGLANG_MAX_RUNNING_REQUESTS="${SGLANG_MAX_RUNNING_REQUESTS:-1}"
  SGLANG_MAX_QUEUED_REQUESTS="${SGLANG_MAX_QUEUED_REQUESTS:-0}"
  SGLANG_EXTRA_ARGS="${SGLANG_EXTRA_ARGS:-}"
  SGLANG_TTS_MEM_FRACTION_STATIC="${SGLANG_TTS_MEM_FRACTION_STATIC:-0.45}"
  SGLANG_TTS_MAX_RUNNING_REQUESTS="${SGLANG_TTS_MAX_RUNNING_REQUESTS:-1}"
  SGLANG_TTS_MAX_NEW_TOKENS="${SGLANG_TTS_MAX_NEW_TOKENS:-512}"
  SGLANG_TTS_TORCH_COMPILE="${SGLANG_TTS_TORCH_COMPILE:-0}"
  SGLANG_TTS_CUDA_GRAPH="${SGLANG_TTS_CUDA_GRAPH:-0}"
  SGLANG_STARTUP_TIMEOUT_SECONDS="${SGLANG_STARTUP_TIMEOUT_SECONDS:-900}"
  WORKER_MANAGE_SGLANG="${WORKER_MANAGE_SGLANG:-1}"
  HEARTBEAT_INTERVAL_SECONDS="${HEARTBEAT_INTERVAL_SECONDS:-5}"

  mkdir -p "${MODEL_DIR}" "${CACHE_DIR}"
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
    printf 'SGLANG_HOST=%s\n' "${SGLANG_HOST}"
    printf 'SGLANG_PORT=%s\n' "${SGLANG_PORT}"
    printf 'SGLANG_CONFIG=%s\n' "${SGLANG_CONFIG}"
    printf 'SGLANG_MAX_RUNNING_REQUESTS=%s\n' "${SGLANG_MAX_RUNNING_REQUESTS}"
    printf 'SGLANG_MAX_QUEUED_REQUESTS=%s\n' "${SGLANG_MAX_QUEUED_REQUESTS}"
    printf 'SGLANG_EXTRA_ARGS=%q\n' "${SGLANG_EXTRA_ARGS}"
    printf 'SGLANG_TTS_MEM_FRACTION_STATIC=%s\n' "${SGLANG_TTS_MEM_FRACTION_STATIC}"
    printf 'SGLANG_TTS_MAX_RUNNING_REQUESTS=%s\n' "${SGLANG_TTS_MAX_RUNNING_REQUESTS}"
    printf 'SGLANG_TTS_MAX_NEW_TOKENS=%s\n' "${SGLANG_TTS_MAX_NEW_TOKENS}"
    printf 'SGLANG_TTS_TORCH_COMPILE=%s\n' "${SGLANG_TTS_TORCH_COMPILE}"
    printf 'SGLANG_TTS_CUDA_GRAPH=%s\n' "${SGLANG_TTS_CUDA_GRAPH}"
    printf 'SGLANG_STARTUP_TIMEOUT_SECONDS=%s\n' "${SGLANG_STARTUP_TIMEOUT_SECONDS}"
    printf 'WORKER_MANAGE_SGLANG=%s\n' "${WORKER_MANAGE_SGLANG}"
    printf 'HEARTBEAT_INTERVAL_SECONDS=%s\n' "${HEARTBEAT_INTERVAL_SECONDS}"
  } >"${env_file}"
  log "wrote ${env_file}"
}

setup_worker_venv() {
  local worker_dir="$1"
  log "setting up worker Python environment"
  cd "${worker_dir}"
  if [ -x .venv/bin/python ]; then
    log "reusing existing worker venv"
  else
    rm -rf .venv
    uv venv .venv -p "${PYTHON_BIN}" --system-site-packages
  fi
  UV_INDEX_URL="${PYPI_INDEX_URL}" UV_DEFAULT_INDEX="${PYPI_INDEX_URL}" PIP_INDEX_URL="${PYPI_INDEX_URL}" uv sync --no-dev --inexact
  uv pip install --python "${worker_dir}/.venv/bin/python" --index-url "${PYPI_INDEX_URL}" --upgrade pip setuptools wheel packaging ninja
}

torch_status() {
  local python_bin="$1"
  "${python_bin}" - <<'PY'
try:
    import torch
except Exception:
    print("missing")
    raise SystemExit(0)

cuda_version = torch.version.cuda or ""
major_minor = tuple(int(part) for part in torch.__version__.split("+", 1)[0].split(".")[:2])
ok = major_minor in {(2, 8), (2, 9)} and cuda_version.startswith("12.8") and torch.cuda.is_available()
print("ok" if ok else "bad")
PY
}

install_torch_if_needed() {
  local python_bin="$1"
  local status
  status="$(torch_status "${python_bin}")"

  if [ "${INSTALL_TORCH}" = "0" ]; then
    if [ "${status}" != "ok" ]; then
      printf 'PyTorch CUDA environment is not compatible and INSTALL_TORCH=0.\n' >&2
      exit 1
    fi
    log "using existing PyTorch CUDA environment"
    return 0
  fi

  if [ "${INSTALL_TORCH}" = "1" ] || [ "${status}" != "ok" ]; then
    log "installing PyTorch CUDA wheels from ${PYTORCH_INDEX_URL}"
    # shellcheck disable=SC2086
    uv pip install --python "${python_bin}" --index-url "${PYTORCH_INDEX_URL}" --upgrade --force-reinstall ${TORCH_PACKAGES}
  else
    log "reusing compatible PyTorch CUDA environment"
  fi
}

install_sglang_omni() {
  local python_bin="$1"
  local generated_overrides_file=""

  if [ "${INSTALL_SGLANG}" = "0" ]; then
    log "INSTALL_SGLANG=0; skip SGLang installation"
    return 0
  fi

  if [ "${INSTALL_FLASHINFER}" = "1" ]; then
    log "installing flashinfer-python from ${FLASHINFER_INDEX_URL}"
    uv pip install --python "${python_bin}" --index-url "${FLASHINFER_INDEX_URL}" flashinfer-python || \
      log "warning: flashinfer-python install failed; continuing without it"
  fi

  if [ -n "${SGLANG_INSTALL_SPEC}" ]; then
    log "installing ${SGLANG_INSTALL_SPEC}"
    uv pip install --python "${python_bin}" --index-url "${PYPI_INDEX_URL}" --upgrade "${SGLANG_INSTALL_SPEC}"
    return 0
  fi

  if [ "${INSTALL_SGLANG_OMNI}" = "0" ]; then
    log "INSTALL_SGLANG_OMNI=0; skip SGLang-Omni installation"
    return 0
  fi

  if [ ! -d "${SGLANG_OMNI_DIR}/.git" ]; then
    mkdir -p "$(dirname "${SGLANG_OMNI_DIR}")"
    rm -rf "${SGLANG_OMNI_DIR}"
    local attempt=1
    while [ "${attempt}" -le "${GIT_RETRY_ATTEMPTS}" ]; do
      log "cloning SGLang-Omni to ${SGLANG_OMNI_DIR} (attempt ${attempt}/${GIT_RETRY_ATTEMPTS})"
      if github_accelerated_git -c http.version=HTTP/1.1 clone --depth 1 --filter=blob:none "${SGLANG_OMNI_REPO}" "${SGLANG_OMNI_DIR}"; then
        break
      fi
      rm -rf "${SGLANG_OMNI_DIR}"
      attempt=$((attempt + 1))
      sleep 5
    done
    if [ ! -d "${SGLANG_OMNI_DIR}/.git" ]; then
      printf 'Failed to clone SGLang-Omni after %s attempts.\n' "${GIT_RETRY_ATTEMPTS}" >&2
      exit 1
    fi
  fi

  log "checking out SGLang-Omni ${SGLANG_OMNI_REF}"
  github_accelerated_git -C "${SGLANG_OMNI_DIR}" -c http.version=HTTP/1.1 fetch --depth 1 origin "${SGLANG_OMNI_REF}" || true
  git -C "${SGLANG_OMNI_DIR}" checkout "${SGLANG_OMNI_REF}"
  github_accelerated_git -C "${SGLANG_OMNI_DIR}" -c http.version=HTTP/1.1 pull --ff-only || true

  log "installing SGLang-Omni into worker venv"
  local override_args=()
  if [ -n "${SGLANG_OMNI_UV_OVERRIDES_FILE}" ]; then
    override_args=(--overrides "${SGLANG_OMNI_UV_OVERRIDES_FILE}")
  elif [ -n "${SGLANG_OMNI_UV_OVERRIDES}" ]; then
    generated_overrides_file="$(mktemp)"
    printf '%s\n' "${SGLANG_OMNI_UV_OVERRIDES}" >"${generated_overrides_file}"
    override_args=(--overrides "${generated_overrides_file}")
  fi

  UV_INDEX_URL="${PYPI_INDEX_URL}" UV_DEFAULT_INDEX="${PYPI_INDEX_URL}" PIP_INDEX_URL="${PYPI_INDEX_URL}" uv pip install --python "${python_bin}" "${override_args[@]}" -v -e "${SGLANG_OMNI_DIR}"
  if [ -n "${generated_overrides_file}" ]; then
    rm -f "${generated_overrides_file}"
  fi
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
  setup_worker_venv "${repo_root}/fish-worker"
  install_torch_if_needed "${repo_root}/fish-worker/.venv/bin/python"
  install_sglang_omni "${repo_root}/fish-worker/.venv/bin/python"
  write_worker_env "${repo_root}"
  download_model "${repo_root}/fish-worker"

  log "done"
  log "start worker:  bash ${repo_root}/scripts/autodl_start_worker.sh"
}

main "$@"
