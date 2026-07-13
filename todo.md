# Worker GPU Fault Auto-Recovery Plan

## Implemented now

- Make worker heartbeat GPU-aware.
  - Keep reporting GPU metrics from `nvidia-smi`.
  - Treat missing GPU/NVML failure as unhealthy when `WORKER_REQUIRE_GPU=1`.
  - Surface GPU/CUDA failures in `last_error` so manager can avoid routing to a bad worker.

- Add a two-level local watchdog.
  - Light failures: Fish API server health failures, request connection failures, and inference HTTP 5xx responses increment an API server failure counter.
  - After `API_SERVER_WATCHDOG_FAILURES_BEFORE_RESTART` consecutive light failures, restart only the managed Fish API server child process.
  - Heavy failures: missing GPU, failed `nvidia-smi`, or failed CUDA tiny-op increment a GPU failure counter.
  - After `GPU_WATCHDOG_FAILURES_BEFORE_EXIT` consecutive heavy failures, exit the worker with code `70` so Docker Compose can recreate the container.

- Stop trusting only `/v1/health`.
  - Deep health now requires `/v1/health` plus a successful CUDA tiny operation:
    `torch.empty(1, device="cuda").sum().item()`.
  - A real short-text dry-run TTS probe is intentionally deferred to avoid adding service load now.

## Later

- Manager-side routing guard.
  - If a GPU-required worker reports `gpu_count=0`, mark it unavailable.
  - If a worker repeatedly returns `sglang_unavailable`, temporarily pause routing to it.

- Host-side Xid watcher.
  - Watch host kernel logs for `NVRM: Xid`.
  - Restart `fish-worker` when severe GPU faults are attributed to the worker/SGLang process.

- Optional deeper model probe.
  - Add a low-frequency, short-text dry-run TTS probe after measuring latency and GPU memory impact.
