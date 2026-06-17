# AutoDL S2-Pro Worker Tuning

This note records the worker settings tested with Fish API server and `fishaudio/s2-pro`, plus the knobs to tune for speed, concurrency, and longer generations.

## Verified Baseline

4090D 24GB, tested 2026-06-16:

```bash
API_SERVER_COMPILE=1
API_SERVER_HALF=0
API_SERVER_MAX_RUNNING_REQUESTS=1
API_SERVER_MAX_QUEUED_REQUESTS=0
API_SERVER_TTS_MAX_NEW_TOKENS=1024
API_SERVER_WORKERS=1
PYTORCH_ALLOC_CONF=expandable_segments:True
```

Result:

- Fish API server `/v1/health` became ready.
- Direct `/v1/tts` returned `200 OK` and valid WAV.
- Default bf16 with `--compile` completed 240 to 248 Chinese characters on the tested 4090D.
- 320 Chinese characters failed with decoder CUDA OOM on the tested 4090D.
- `--half` did not show a stable speed or max-length benefit in this test, so it remains disabled by default.

Approximate direct API server single-request results on 4090D:

| Mode | Chars | Wall Time | WAV Duration | Result |
| --- | ---: | ---: | ---: | --- |
| `--compile --half` | 80 | 13.43s | 18.95s | OK |
| `--compile --half` | 160 | 28.91s | 48.02s | OK |
| `--compile --half` | 245 | 39.56s | 64.88s | OK |
| `--compile --half` | 248 | n/a | n/a | OOM |
| `--compile` bf16 | 240 | 38.46s | 62.74s | OK |
| `--compile` bf16 | 248 | 35.61s | 58.56s | OK |
| `--compile` bf16 | 320 | n/a | n/a | OOM |

## Important Knobs

`API_SERVER_MAX_RUNNING_REQUESTS`

Controls worker-local running requests. Keep this at `1` on 24GB GPUs unless you have verified VRAM headroom. The worker enforces this limit before forwarding to Fish API server.

`API_SERVER_MAX_QUEUED_REQUESTS`

Controls worker-local queued requests. Set `0` to reject overload immediately. When the queue is full, the worker returns retryable `overloaded` to the manager.

`API_SERVER_TTS_MAX_NEW_TOKENS`

Sets the default output-token limit for worker-forwarded requests. A client-provided `max_new_tokens` still wins. Lower values reduce worst-case decode time and help avoid long-request memory pressure. Higher values allow longer audio but can increase latency and OOM risk.

`API_SERVER_COMPILE`

Starts `tools/api_server.py` with `--compile`. This is the recommended default for the tested Fish API server path.

`API_SERVER_HALF`

Starts `tools/api_server.py` with `--half`, changing precision from default bfloat16 to float16. Keep this off unless your own benchmark shows it is stable for your GPU and text lengths.

`API_SERVER_WORKERS`

Uvicorn worker count. Keep `1` unless each worker process can fit a full model copy in VRAM.

`API_SERVER_MAX_TEXT_LENGTH`

Server-side text length guard. `0` disables it. On 24GB GPUs, use around `240` if you want to reject risky long requests before generation.

## Recommended Startup

Default paid-instance startup for 24GB GPUs:

```bash
API_SERVER_COMPILE=1 \
API_SERVER_HALF=0 \
API_SERVER_MAX_RUNNING_REQUESTS=1 \
API_SERVER_MAX_QUEUED_REQUESTS=0 \
API_SERVER_TTS_MAX_NEW_TOKENS=1024 \
API_SERVER_WORKERS=1 \
PYTORCH_ALLOC_CONF=expandable_segments:True \
bash scripts/autodl_start_worker.sh
```

Conservative long-text guard:

```bash
API_SERVER_MAX_TEXT_LENGTH=240 bash scripts/autodl_start_worker.sh
```

Allow a small local queue:

```bash
API_SERVER_MAX_QUEUED_REQUESTS=2 bash scripts/autodl_start_worker.sh
```

This queues inside the worker, not inside the API server. Requests beyond `running + queued` are rejected as retryable overloads.

## Suggested Test Loop

After each config change, run a health and TTS check before sending real traffic:

```bash
curl -fsS http://127.0.0.1:8000/v1/health
curl -sS -D run/tts.headers -o run/tts.wav \
  -X POST http://127.0.0.1:8000/v1/tts \
  -H "Content-Type: application/json" \
  --data '{"text":"Hello, this is a short test.","format":"wav","max_new_tokens":128}' \
  --max-time 300
nvidia-smi --query-gpu=memory.used,memory.total,utilization.gpu --format=csv,noheader
```

For length tests, do not trust HTTP `200` alone. Check WAV duration and confirm duration grows as text gets longer; a flat duration can indicate output-token truncation.

Stored manager voices are written directly to Fish API server's `references/{reference_id}/sample.*` and `sample.lab` format on first use, then reused by `reference_id`. Repeated calls should not resend reference audio bytes. The first request for a voice can be slower because it may download the voice from the manager and write the API server reference files.

## What To Try If Startup OOMs

Try these in order:

1. Set `API_SERVER_HALF=0`.
2. Set `API_SERVER_WORKERS=1`.
3. Set `API_SERVER_COMPILE=0`.
4. Lower `API_SERVER_TTS_MAX_NEW_TOKENS`, for example `1024 -> 512`.
5. Keep `API_SERVER_MAX_RUNNING_REQUESTS=1` and `API_SERVER_MAX_QUEUED_REQUESTS=0`.
6. Check for orphan GPU processes with `nvidia-smi` and stop only stale worker/API server processes from the previous run.

## What To Try If Requests Are Slow

Try these after the safe baseline is stable:

1. Keep the model process warm; the first request after startup is usually slower.
2. Keep `API_SERVER_COMPILE=1` and measure repeated-request latency after warmup.
3. Use shorter `max_new_tokens` for short-form requests.
4. Split long text into chunks rather than pushing risky single long requests.
5. Increase `API_SERVER_MAX_RUNNING_REQUESTS` only if throughput matters more than single-request latency and VRAM is stable.

## Cleanup On Paid Machines

Before stopping or imaging a paid AutoDL machine, verify no orphan GPU process remains:

```bash
nvidia-smi
```

If the test worker was started with `scripts/autodl_start_worker.sh`, stop its process tree or terminate the matching worker/API server commands. Avoid broad destructive cleanup if other users or jobs are sharing the machine.
