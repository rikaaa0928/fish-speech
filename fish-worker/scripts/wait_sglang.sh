#!/usr/bin/env bash
set -euo pipefail

SGLANG_HOST="${SGLANG_HOST:-127.0.0.1}"
SGLANG_PORT="${SGLANG_PORT:-8000}"
TIMEOUT_SECONDS="${SGLANG_STARTUP_TIMEOUT_SECONDS:-900}"
DEADLINE=$((SECONDS + TIMEOUT_SECONDS))

until curl -fsS "http://${SGLANG_HOST}:${SGLANG_PORT}/health" >/dev/null 2>&1 || curl -fsS "http://${SGLANG_HOST}:${SGLANG_PORT}/v1/models" >/dev/null 2>&1; do
  if [ "${SECONDS}" -ge "${DEADLINE}" ]; then
    echo "SGLang did not become healthy before timeout" >&2
    exit 1
  fi
  sleep 2
done

echo "SGLang is healthy"
