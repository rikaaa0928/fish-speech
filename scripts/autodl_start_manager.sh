#!/usr/bin/env bash
set -euo pipefail

export PATH="${HOME}/.cargo/bin:${HOME}/.local/bin:${PATH}"

AUTODL_FS="${AUTODL_FS:-/root/autodl-fs}"
REPO_DIR="${REPO_DIR:-${AUTODL_FS}/fish-speech}"
REPO_REF="${REPO_REF:-}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
MANAGER_PROFILE="${MANAGER_PROFILE:-release}"
OVERWRITE_ENV="${OVERWRITE_ENV:-0}"
RUSTUP_DIST_SERVER="${RUSTUP_DIST_SERVER:-https://mirrors.ustc.edu.cn/rust-static}"
RUSTUP_UPDATE_ROOT="${RUSTUP_UPDATE_ROOT:-https://mirrors.ustc.edu.cn/rust-static/rustup}"
RUSTUP_INIT_BASE_URL="${RUSTUP_INIT_BASE_URL:-https://mirrors.ustc.edu.cn/rust-static/rustup/dist}"
CARGO_REGISTRY_URL="${CARGO_REGISTRY_URL:-sparse+https://mirrors.tuna.tsinghua.edu.cn/crates.io-index/}"

log() {
  printf '[autodl-manager] %s\n' "$*" >&2
}

export RUSTUP_DIST_SERVER
export RUSTUP_UPDATE_ROOT
export CARGO_REGISTRIES_CRATES_IO_PROTOCOL="${CARGO_REGISTRIES_CRATES_IO_PROTOCOL:-sparse}"

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

install_system_packages() {
  if ! command -v apt-get >/dev/null 2>&1; then
    return 0
  fi

  log "installing manager build packages"
  run_root apt-get update
  run_root apt-get install -y --no-install-recommends \
    build-essential \
    ca-certificates \
    curl \
    git \
    libssl-dev \
    pkg-config
}

resolve_repo_root() {
  local script_dir
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

  if [ -d "${script_dir}/../fish-manager" ] && [ -d "${script_dir}/../fish-worker" ]; then
    cd "${script_dir}/.." && pwd
    return 0
  fi

  if [ -d "${PWD}/fish-manager" ] && [ -d "${PWD}/fish-worker" ]; then
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

ensure_rust() {
  configure_cargo_mirror

  if command -v cargo >/dev/null 2>&1; then
    return 0
  fi

  local rustup_target rustup_init
  case "$(uname -m)" in
    x86_64|amd64) rustup_target="x86_64-unknown-linux-gnu" ;;
    aarch64|arm64) rustup_target="aarch64-unknown-linux-gnu" ;;
    *)
      printf 'Unsupported CPU architecture for domestic rustup-init mirror: %s\n' "$(uname -m)" >&2
      exit 1
      ;;
  esac

  log "installing Rust toolchain from ${RUSTUP_INIT_BASE_URL}/${rustup_target}/rustup-init"
  rustup_init="$(mktemp)"
  curl --proto '=https' --tlsv1.2 -sSf "${RUSTUP_INIT_BASE_URL}/${rustup_target}/rustup-init" -o "${rustup_init}"
  chmod +x "${rustup_init}"
  "${rustup_init}" -y --profile minimal
  rm -f "${rustup_init}"
  # shellcheck disable=SC1091
  source "${HOME}/.cargo/env"
}

configure_cargo_mirror() {
  local cargo_home="${CARGO_HOME:-${HOME}/.cargo}"
  local cargo_config="${cargo_home}/config.toml"

  mkdir -p "${cargo_home}"
  if [ -f "${cargo_config}" ]; then
    if command -v grep >/dev/null 2>&1 && grep -Fq "${CARGO_REGISTRY_URL}" "${cargo_config}"; then
      return 0
    fi
    if command -v grep >/dev/null 2>&1 && grep -q '^\[source\.crates-io\]' "${cargo_config}"; then
      log "warning: ${cargo_config} already configures crates-io; leaving it unchanged"
      return 0
    fi
  fi

  log "configuring Cargo crates mirror: ${CARGO_REGISTRY_URL}"
  {
    printf '[source.crates-io]\n'
    printf "replace-with = 'mirror'\n\n"
    printf '[source.mirror]\n'
    printf 'registry = "%s"\n\n' "${CARGO_REGISTRY_URL}"
    printf '[registries.mirror]\n'
    printf 'index = "%s"\n' "${CARGO_REGISTRY_URL}"
  } >>"${cargo_config}"
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

load_worker_env() {
  local worker_env="$1/fish-worker/.env"
  if [ -f "${worker_env}" ]; then
    set -a
    # shellcheck disable=SC1090
    source "${worker_env}"
    set +a
  fi
}

ensure_manager_env() {
  local repo_root="$1"
  local env_file="${repo_root}/fish-manager/.env"

  if [ -f "${env_file}" ] && [ "${OVERWRITE_ENV}" != "1" ]; then
    log "using existing ${env_file}"
    return 0
  fi

  load_worker_env "${repo_root}"

  WORKER_TOKEN="${WORKER_TOKEN:-$(generate_secret)}"
  OPENAI_API_KEYS="${OPENAI_API_KEYS:-sk-autodl-$(generate_secret)}"
  MANAGER_BIND_ADDR="${MANAGER_BIND_ADDR:-0.0.0.0:8080}"
  SQLITE_PATH="${SQLITE_PATH:-${AUTODL_FS}/fish-manager-data/manager.sqlite3}"
  BLOB_LOCAL_DIR="${BLOB_LOCAL_DIR:-${AUTODL_FS}/fish-manager-data/voices}"
  MANAGER_RETRY_ON_WORKER_OVERLOAD="${MANAGER_RETRY_ON_WORKER_OVERLOAD:-1}"

  mkdir -p "$(dirname "${SQLITE_PATH}")" "${BLOB_LOCAL_DIR}"
  umask 077
  {
    printf 'MANAGER_BIND_ADDR=%s\n' "${MANAGER_BIND_ADDR}"
    printf 'OPENAI_API_KEYS=%s\n' "${OPENAI_API_KEYS}"
    printf 'WORKER_TOKEN=%s\n' "${WORKER_TOKEN}"
    printf 'SQLITE_PATH=%s\n' "${SQLITE_PATH}"
    printf 'BLOB_LOCAL_DIR=%s\n' "${BLOB_LOCAL_DIR}"
    printf 'MANAGER_RETRY_ON_WORKER_OVERLOAD=%s\n' "${MANAGER_RETRY_ON_WORKER_OVERLOAD}"
  } >"${env_file}"
  log "wrote ${env_file}"
}

main() {
  install_system_packages
  local repo_root manager_dir bin
  repo_root="$(resolve_repo_root)"
  manager_dir="${repo_root}/fish-manager"

  ensure_rust
  ensure_manager_env "${repo_root}"

  cd "${manager_dir}"
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
  mkdir -p "$(dirname "${SQLITE_PATH}")" "${BLOB_LOCAL_DIR}"

  if [ "${MANAGER_PROFILE}" = "debug" ]; then
    log "building fish-manager debug binary"
    cargo build
    bin="${manager_dir}/target/debug/fish-manager"
  else
    log "building fish-manager release binary"
    cargo build --release
    bin="${manager_dir}/target/release/fish-manager"
  fi

  export RUST_LOG="${RUST_LOG:-fish_manager=info,tower_http=info}"
  log "starting fish-manager on ${MANAGER_BIND_ADDR}"
  exec "${bin}"
}

main "$@"
