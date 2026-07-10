#!/usr/bin/env bash
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
RUN_DIR="${REPO_ROOT}/run"
RESTART_DELAY_SECONDS="${RESTART_DELAY_SECONDS:-5}"

mkdir -p "${RUN_DIR}"
cd "${REPO_ROOT}"

echo $$ > "${RUN_DIR}/fish-worker-supervisor.pid"

while true; do
  printf '[%s] starting fish worker\n' "$(date -Is)" >> "${RUN_DIR}/fish-worker-supervisor.log"
  bash "${REPO_ROOT}/scripts/autodl_start_worker.sh" >> "${RUN_DIR}/fish-worker.log" 2>&1
  rc=$?
  printf '[%s] fish worker exited rc=%s; restarting in %ss\n' \
    "$(date -Is)" "${rc}" "${RESTART_DELAY_SECONDS}" >> "${RUN_DIR}/fish-worker-supervisor.log"
  sleep "${RESTART_DELAY_SECONDS}"
done
