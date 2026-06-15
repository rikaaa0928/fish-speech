# AutoDL S2-Pro Worker Tuning

This note records the AutoDL worker settings tested with SGLang-Omni and `fishaudio/s2-pro`, plus the knobs to tune for speed, concurrency, and longer generations.

## Verified Baselines

4090D 24GB, tested 2026-06-15:

```bash
SGLANG_TTS_MEM_FRACTION_STATIC=0.35
SGLANG_TTS_MAX_RUNNING_REQUESTS=1
SGLANG_TTS_MAX_NEW_TOKENS=2048
SGLANG_TTS_TORCH_COMPILE=0
SGLANG_TTS_CUDA_GRAPH=0
PYTORCH_ALLOC_CONF=expandable_segments:True
```

Result:

- SGLang `/health` became ready.
- Direct `/v1/audio/speech` returned `200 OK` and a valid WAV.
- Direct SGLang single-request tests completed successfully through about 478 Chinese characters before output duration plateaued.
- Runtime versions were `torch 2.9.1+cu128`, `sglang 0.5.8`, `sglang-omni 0.1.0`, `protobuf 6.33.6`.

The 4090D base image used during testing did not provide `python3` on `PATH` and had system PyTorch `2.5.1+cu124`. The setup script handles that by selecting `/root/miniconda3/bin/python` when present and then installing a CUDA 12.8 PyTorch in the worker venv if needed. No manual Python adaptation is expected for that image shape.

SGLang-Omni `main` can have a dependency resolver conflict between `sglang>=0.5.8` requiring modern `protobuf` and `descript-audiotools` declaring an older `protobuf` range. `scripts/autodl_setup.sh` defaults `SGLANG_OMNI_UV_OVERRIDES` to `protobuf>=6.31.1,<7.0.0` so a fresh 4090D setup does not need the manual `uv --overrides` step used during the first validation. Set `SGLANG_OMNI_UV_OVERRIDES=` to disable this override, or set `SGLANG_OMNI_UV_OVERRIDES_FILE=/path/to/overrides.txt` to use a custom uv overrides file.

4080 SUPER 32GB, tested earlier:

```bash
SGLANG_TTS_MEM_FRACTION_STATIC=0.65
SGLANG_TTS_MAX_RUNNING_REQUESTS=1
SGLANG_TTS_MAX_NEW_TOKENS=1024
SGLANG_TTS_TORCH_COMPILE=0
SGLANG_TTS_CUDA_GRAPH=0
```

Result:

- Worker registered with a local manager.
- End-to-end TTS returned `200 OK` and a valid WAV.
- SGLang load used about `27.6GB / 32GB`.

## Important Knobs

`SGLANG_TTS_MAX_NEW_TOKENS`

Sets the default S2-Pro output-token limit for worker-forwarded requests and writes `tts_engine.max_new_tokens` into the generated SGLang config. The default is `2048` for 24GB GPUs. Lower values reduce worst-case decode time and help avoid long-request memory pressure. Higher values allow longer audio but increase latency and VRAM/KV-cache pressure. If a client request explicitly includes `max_new_tokens`, that request value wins. If you call SGLang directly, include `"max_new_tokens": N` in the JSON request when you want a per-request cap.

`SGLANG_TTS_MEM_FRACTION_STATIC`

Controls SGLang's static memory fraction for the S2-Pro TTS engine. The default is `0.35` for 24GB GPUs. This is the main VRAM tuning knob. Higher values give SGLang more room for KV/cache and can support longer outputs or more concurrency, but can OOM during model/vocoder load if too high. Lower values leave more headroom for CUDA libraries, vocoder, fragmentation, and other processes, but may limit long outputs or concurrent requests.

`SGLANG_TTS_MAX_RUNNING_REQUESTS`

Controls the S2-Pro TTS engine's internal SGLang concurrency. Keep this at `1` until the single-request baseline is stable. Increase it only when there is clear VRAM headroom and you need throughput more than per-request latency.

`SGLANG_MAX_RUNNING_REQUESTS` and `SGLANG_MAX_QUEUED_REQUESTS`

These are the worker-advertised SGLang limits and are used when starting without a SGLang config. With the S2-Pro config path, the TTS-specific `SGLANG_TTS_MAX_RUNNING_REQUESTS` is the important engine override. Keep the advertised values aligned with the real TTS engine limit so the manager does not overroute traffic.

`SGLANG_TTS_TORCH_COMPILE`

Enables or disables S2-Pro's torch compile path. `0` is safest and avoids long startup/compile cost. `1` can improve repeated decode speed after warmup, but startup is slower, extra compile workers may appear, and memory pressure can increase. Only try it after the non-compiled baseline is stable.

`SGLANG_TTS_CUDA_GRAPH`

Enables or disables CUDA graph capture. `0` is safest for small GPUs and avoids graph memory overhead. `1` can improve steady-state latency/throughput, but it may require more VRAM and longer startup.

`SGLANG_EXTRA_ARGS`

Passes extra command-line args to `sgl-omni serve`. Use this only for SGLang flags that are known to be supported by the selected SGLang-Omni pipeline. Unsupported pipeline flags can fail startup.

## Generated Config

`scripts/autodl_start_worker.sh` copies the upstream S2-Pro config and writes a generated config at:

```bash
fish-worker/.generated/s2pro_tts.yaml
```

For a 24GB baseline it should look like:

```yaml
config_cls: S2ProPipelineConfig
model_path: /autodl-fs/data/models/s2-pro
relay_backend: shm
runtime_overrides:
  tts_engine:
    max_new_tokens: 2048
    server_args_overrides:
      mem_fraction_static: 0.35
      max_running_requests: 1
      enable_torch_compile: false
      disable_cuda_graph: true
```

S2-Pro's SGLang-Omni stage sets `dtype: "bfloat16"` by default in `sglang_omni/models/fishaudio_s2_pro/stages.py`, so the tested path is already bf16.

## Tuning Recipes

Default paid-instance startup for 24GB GPUs:

```bash
SGLANG_TTS_MEM_FRACTION_STATIC=0.35 \
SGLANG_TTS_MAX_RUNNING_REQUESTS=1 \
SGLANG_TTS_MAX_NEW_TOKENS=2048 \
SGLANG_TTS_TORCH_COMPILE=0 \
SGLANG_TTS_CUDA_GRAPH=0 \
PYTORCH_ALLOC_CONF=expandable_segments:True \
bash scripts/autodl_start_worker.sh
```

More speed for repeated requests:

```bash
SGLANG_TTS_TORCH_COMPILE=1
SGLANG_TTS_CUDA_GRAPH=1
```

Try these one at a time. Measure startup time, first request latency, repeated request latency, and VRAM. Revert the one that causes OOM or unstable startup.

More throughput/concurrency:

```bash
SGLANG_TTS_MAX_RUNNING_REQUESTS=2
SGLANG_MAX_RUNNING_REQUESTS=2
```

Raise concurrency gradually: `1 -> 2 -> 4`. Keep the advertised SGLang concurrency aligned with the TTS engine concurrency. If requests become much slower, GPU utilization is low, or OOM occurs, go back down.

Longer generation than the default:

```bash
SGLANG_TTS_MAX_NEW_TOKENS=3072
```

Try values above `2048` only after the default is stable. Longer outputs increase decode time roughly with output length and may require more VRAM headroom. On 24GB, test cautiously; long single requests may be better handled by splitting text into chunks.

Text length notes:

The benchmark reports text length as Python `len(text)`, so the values are Unicode character counts, not UTF-8 byte counts. In the 24GB `0.35/2048` validation, direct SGLang output duration grew through about `478` Chinese characters and plateaued around `543`, indicating truncation after the effective limit. Use this as a validated reference point, not as a hard maximum.

Lower latency for short prompts:

```bash
SGLANG_TTS_MAX_NEW_TOKENS=256
```

This caps default worker requests to short outputs. Clients that need longer audio can still pass a larger per-request `max_new_tokens`.

## Suggested Test Loop

After each config change, run a small health and TTS check before sending real traffic:

```bash
curl -fsS http://127.0.0.1:8000/health
curl -sS -D run/tts.headers -o run/tts.wav \
  -X POST http://127.0.0.1:8000/v1/audio/speech \
  -H "Content-Type: application/json" \
  --data '{"input":"Hello, this is a short test.","response_format":"wav","max_new_tokens":128}' \
  --max-time 300
nvidia-smi --query-gpu=memory.used,memory.total,utilization.gpu --format=csv,noheader
```

For manager/worker end-to-end tests, send the same `max_new_tokens` field through the public request body. The manager preserves extra fields and the worker forwards them to SGLang.

## What To Try If Startup OOMs

Try these in order:

1. Set `SGLANG_TTS_TORCH_COMPILE=0`.
2. Set `SGLANG_TTS_CUDA_GRAPH=0`.
3. Lower `SGLANG_TTS_MEM_FRACTION_STATIC`, for example `0.35 -> 0.32` on 24GB.
4. Lower `SGLANG_TTS_MAX_NEW_TOKENS`, for example `2048 -> 1024`.
5. Keep all concurrency at `1` and all queues at `0`.
6. Check for orphan GPU processes with `nvidia-smi` and stop only stale worker/SGLang processes from the previous run.

## What To Try If Requests Are Slow

Try these after the safe baseline is stable:

1. Keep the model process warm; the first request after startup is usually slower.
2. Use shorter `max_new_tokens` for short-form requests.
3. Try `SGLANG_TTS_TORCH_COMPILE=1` and measure repeated-request latency after warmup.
4. Try `SGLANG_TTS_CUDA_GRAPH=1` if there is VRAM headroom.
5. Increase `SGLANG_TTS_MAX_RUNNING_REQUESTS` only if throughput matters more than single-request latency.

## Cleanup On Paid Machines

Before stopping or imaging a paid AutoDL machine, verify no orphan GPU process remains:

```bash
nvidia-smi
```

If the test worker was started with `scripts/autodl_start_worker.sh`, stop its process tree or terminate the matching worker/SGLang commands. Avoid broad destructive cleanup if other users or jobs are sharing the machine.
