#!/usr/bin/env bash
set -euo pipefail

API_SERVER_HOST="${API_SERVER_HOST:-127.0.0.1}"
API_SERVER_PORT="${API_SERVER_PORT:-8000}"
TIMEOUT_SECONDS="${API_SERVER_STARTUP_TIMEOUT_SECONDS:-900}"
STARTED_AT="$(date +%s)"

until curl -fsS "http://${API_SERVER_HOST}:${API_SERVER_PORT}/v1/health" >/dev/null 2>&1; do
  if [ $(( $(date +%s) - STARTED_AT )) -ge "${TIMEOUT_SECONDS}" ]; then
    echo "Fish API server did not become healthy before timeout" >&2
    exit 1
  fi
  sleep 2
done

echo "Fish API server is healthy"
