# TTS Validation

This directory contains the repeatable TTS validation flow used for worker tuning.

## Configuration

Create or update `fish-manager/.env`, or pass the same values as CLI flags.

```bash
FISH_MANAGER_BASE_URL=https://<manager-host>
OPENAI_API_KEY=<public-api-key>
# or: OPENAI_API_KEYS=<public-api-key-1>,<public-api-key-2>

# Stored reference voice used by validation requests.
FISH_MANAGER_BENCH_VOICE_ID=leijun

# Required only for tune-remote.
WORKER_TOKEN=<worker-token>
FISH_MANAGER_WORKER_WS_URL=wss://<manager-host>/internal/workers/ws
FISH_MANAGER_BENCH_SSH_HOST=<user>@<remote-host>
FISH_MANAGER_BENCH_SSH_PORT=22
FISH_MANAGER_BENCH_SSH_KEY=/path/to/private/key
FISH_MANAGER_BENCH_REMOTE_REPO=/root/src/fish-speech
FISH_MANAGER_BENCH_REMOTE_REF=rev-agent
```

`tts_bench.py` loads `fish-manager/.env` by default. Public API auth uses `OPENAI_API_KEY`, or the first value from `OPENAI_API_KEYS`. Worker auth uses `WORKER_TOKEN` only for remote worker startup.

Generated audio and results are written under `fish-manager/bench_tts/run/`, which is ignored by git.

## Smoke Check

Use the manager API check before benchmarking. This verifies public auth, worker availability, stored voice lookup, and that a generated audio file can be saved locally.

```bash
uv run --script fish-manager/scripts/check_apis.py \
  --only tts \
  --voice-id leijun \
  --timeout 300 \
  --require-worker
```

Expected result: `POST /v1/audio/speech` returns `200 OK` and the script prints the saved audio path.

## Local Benchmark

Run this when a worker is already connected to the configured manager.

```bash
uv run --script fish-manager/bench_tts/tts_bench.py bench \
  --targets 80,240,480 \
  --repeat 1 \
  --concurrency 1 \
  --timeout 1200 \
  --max-new-tokens 1024 \
  --run-label defaults-check
```

The benchmark writes per-request JSON lines when `--results-path` is set, saves audio unless `--no-audio` is passed, and prints a summary containing success count, latency percentiles, characters per second, and audio realtime factor.

## Remote Tuning

Use `tune-remote` when the script should restart a remote AutoDL worker through SSH for each candidate configuration.

```bash
uv run --script fish-manager/bench_tts/tts_bench.py tune-remote \
  --mem-fractions 0.45,0.50,0.55,0.60 \
  --max-new-tokens-values 512,768,1024 \
  --tune-targets 80,240,480 \
  --tune-repeat 1 \
  --ready-timeout 300 \
  --timeout 1200
```

For each candidate, the script stops stale worker/SGLang processes owned by the remote repo, clears the generated SGLang config, starts `scripts/autodl_start_worker.sh` with the candidate env, waits for a ready worker through `/v1/workers`, then runs the local benchmark flow.

## 24GB Default Validation

The current 24GB default candidate is:

```bash
SGLANG_TTS_MEM_FRACTION_STATIC=0.50
SGLANG_TTS_MAX_RUNNING_REQUESTS=1
SGLANG_TTS_MAX_NEW_TOKENS=1024
SGLANG_TTS_TORCH_COMPILE=0
SGLANG_TTS_CUDA_GRAPH=0
```

A practical validation pass is:

```bash
uv run --script fish-manager/bench_tts/tts_bench.py tune-remote \
  --mem-fractions 0.50 \
  --max-new-tokens-values 1024 \
  --tune-targets 80,240,480 \
  --tune-repeat 2 \
  --ready-timeout 300 \
  --timeout 1200 \
  --run-label defaults-check
```

Accept the configuration when all requests succeed, generated audio files are valid, and no worker-side OOM appears in the remote `fish-worker/run/bench-worker.log`.

## Observed Text Length

The benchmark length targets are character counts, not UTF-8 byte counts. `tts_bench.py` records sample length with Python `len(text)`, so Chinese text is counted as Unicode characters plus punctuation. For UTF-8 byte size, most Chinese characters are about 3 bytes each, so a 465-character Chinese sample is roughly 1395 bytes plus any ASCII or punctuation differences.

With the 24GB default candidate `SGLANG_TTS_MEM_FRACTION_STATIC=0.50` and `SGLANG_TTS_MAX_NEW_TOKENS=1024`, the tested single-request sample range was about 64, 226, and 465 Chinese characters. The mixed benchmark ran these sample sizes twice with one inflight request and all 6 requests succeeded.

Treat about 465 Chinese characters as the current validated single-request reference point for this 24GB setup, not a hard protocol limit. Longer single requests should be tested separately; for production long-form TTS, split text into chunks and concatenate audio instead of raising `SGLANG_TTS_MAX_NEW_TOKENS` indefinitely.

## Troubleshooting

If the script reports a missing API key, set `OPENAI_API_KEY` or `OPENAI_API_KEYS` in `fish-manager/.env`.

If `tune-remote` reports a missing SSH host, set `FISH_MANAGER_BENCH_SSH_HOST` or pass `--ssh-host`.

If no ready worker appears, check manager `/v1/workers` and the remote worker log at `fish-worker/run/bench-worker.log`.

For very long text, prefer splitting text into chunks instead of raising `SGLANG_TTS_MAX_NEW_TOKENS` indefinitely on 24GB GPUs.
