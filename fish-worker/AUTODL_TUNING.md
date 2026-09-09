# AutoDL S2-Pro Worker Tuning

This note records the worker settings tested with Fish API server and `fishaudio/s2-pro`, plus the knobs to tune for speed, concurrency, and longer generations.

## Current 24GB Default

The current FP32 codec default, validated on RTX 3090 24GB with one concurrent request, is:

```bash
API_SERVER_LLAMA_MAX_SEQ_LEN=8192
API_SERVER_TTS_MAX_NEW_TOKENS=4096
API_SERVER_MAX_RUNNING_REQUESTS=1
API_SERVER_WORKERS=1
```

The application layer should limit normal Chinese input to about 400 characters. Generation-limit truncation returns HTTP 206 with `X-TTS-Error-Code: tts_output_truncated`; GPU OOM returns an explicit `tts_out_of_memory` error through the manager.

## Historical Baseline

4090D 24GB, tested 2026-06-16:

```bash
API_SERVER_COMPILE=1
API_SERVER_HALF=0
API_SERVER_MAX_RUNNING_REQUESTS=1
API_SERVER_MAX_QUEUED_REQUESTS=1
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

## RTX 4070 12GB Profile

The following profile was validated on an RTX 4070 SUPER 12GB with
`fishaudio/s2-pro` and one request at a time:

```bash
API_SERVER_COMPILE=1
API_SERVER_HALF=0
API_SERVER_DECODER_DTYPE=bfloat16
API_SERVER_LLAMA_MAX_SEQ_LEN=2560
API_SERVER_TTS_MAX_NEW_TOKENS=512
API_SERVER_MAX_RUNNING_REQUESTS=1
API_SERVER_WORKERS=1
PYTORCH_ALLOC_CONF=expandable_segments:True
```

`--compile` sustained about 26 generated tokens/s after warmup, versus about
11 tokens/s without compilation. The model and BF16 DAC occupied about 9.6 GiB
after warmup.

Ordinary untagged text is deliberately not split inside Fish Speech. The caller
must split it before submitting requests. With the `shantianfang` reference,
fixed seed, and the profile above, a representative 96-character Chinese input
completed as one batch (497 generated tokens), while the corresponding
97-character input reached the 512-token generation cap and the DAC decode ran
out of memory. This boundary depends on text, pronunciation, and sampling, so
use at most 70 Chinese characters per external segment rather than treating 96
as a safe production limit. Prefer sentence punctuation and retain punctuation
when splitting.

The rootless Podman deployment uses NVIDIA CDI (`nvidia.com/gpu=all`) and omits
`shm_size` when `ipc: host` is set. Enable user lingering if the worker must
survive the last SSH logout:

```bash
sudo loginctl enable-linger "$USER"
```

## RTX 4070 12GB FP8 Profile

The `drbaph/s2-pro-fp8` checkpoint was validated through the complete
Manager -> Worker -> Fish API server path on the same RTX 4070 SUPER, including
the `shantianfang` stored reference. Keep the public model ID compatible while
downloading the quantized artifact separately:

```bash
MODEL_ID=fishaudio/s2-pro
MODEL_DOWNLOAD_ID=drbaph/s2-pro-fp8
MODEL_DIR=/models/s2-pro-fp8
MODEL_REQUIRED_FILES="codec.pth model.safetensors"
API_SERVER_COMPILE=1
API_SERVER_HALF=0
API_SERVER_DECODER_DTYPE=bfloat16
API_SERVER_LLAMA_MAX_SEQ_LEN=2560
API_SERVER_TTS_MAX_NEW_TOKENS=1280
API_SERVER_MAX_RUNNING_REQUESTS=1
API_SERVER_WORKERS=1
PYTORCH_ALLOC_CONF=expandable_segments:True
```

The FP8 loader detects scaled E4M3 tensors from checkpoint metadata. It streams
201 quantized linear layers to the GPU and ignores the checkpoint's derived
32768-token buffers, including its roughly 1 GiB causal mask. LLAMA plus KV
cache used about 5.09 GiB; adding the BF16 DAC brought idle allocation to about
5.82 GiB. Hot generation sustained 45.1-45.3 tokens/s, compared with about 26
tokens/s for the compiled BF16 checkpoint on this GPU.

With a 44-second `shantianfang` reference and fixed seed, 150, 180, 190, and
200 representative Chinese characters completed when given enough per-request
generation budget. 210 and 220 characters reached their configured generation
caps. The conservative `2560/1280` profile accepted a 300-character request,
returned HTTP 206 after 1280 generated tokens, and did not OOM. Treat about 300
ordinary Chinese characters as a conservative accepted-input target for this
specific long reference, and about 160-180 characters as a conservative target
for complete single-request audio. Both limits vary with reference duration,
text tokenization, pronunciation, and sampling. Production callers should
still split externally at sentence boundaries, preferably at no more than 70
Chinese characters per segment.

## Important Knobs

`API_SERVER_MAX_RUNNING_REQUESTS`

Controls worker-local running requests. Keep this at `1` on 24GB GPUs unless you have verified VRAM headroom. The worker enforces this limit before forwarding to Fish API server.

`API_SERVER_MAX_QUEUED_REQUESTS`

Controls worker-local queued requests. The default is `1`. Set `0` to reject overload immediately. When the queue is full, the worker returns retryable `overloaded` to the manager.

`API_SERVER_TTS_MAX_NEW_TOKENS`

Sets the default output-token limit for worker-forwarded requests. A client-provided `max_new_tokens` still wins. Lower values reduce worst-case decode time and help avoid long-request memory pressure. Higher values allow longer audio but can increase latency and OOM risk.

`API_SERVER_COMPILE`

Starts `tools/api_server.py` with `--compile`. This is the recommended default for the tested Fish API server path.

`API_SERVER_HALF`

Starts `tools/api_server.py` with `--half`, changing precision from default bfloat16 to float16. Keep this off unless your own benchmark shows it is stable for your GPU and text lengths.

`API_SERVER_WORKERS`

Uvicorn worker count. Keep `1` unless each worker process can fit a full model copy in VRAM.

`API_SERVER_MAX_TEXT_LENGTH`

Server-side text length guard. `0` disables it. For the current `8192/4096` FP32 configuration, limit normal Chinese input to about `400` characters at the application layer.

## Recommended Startup

Default paid-instance startup for 24GB GPUs:

```bash
API_SERVER_COMPILE=1 \
API_SERVER_HALF=0 \
API_SERVER_MAX_RUNNING_REQUESTS=1 \
API_SERVER_MAX_QUEUED_REQUESTS=1 \
API_SERVER_TTS_MAX_NEW_TOKENS=4096 \
API_SERVER_LLAMA_MAX_SEQ_LEN=8192 \
API_SERVER_WORKERS=1 \
PYTORCH_ALLOC_CONF=expandable_segments:True \
bash scripts/autodl_start_worker.sh
```

Conservative long-text guard:

```bash
API_SERVER_MAX_TEXT_LENGTH=400 bash scripts/autodl_start_worker.sh
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
4. Lower `API_SERVER_TTS_MAX_NEW_TOKENS`, for example `4096 -> 3072`.
5. Keep `API_SERVER_MAX_RUNNING_REQUESTS=1`; lower `API_SERVER_MAX_QUEUED_REQUESTS` to `0` if overload should be rejected immediately.
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
