# fish-worker

Outbound GPU worker bridge for `fish-manager` and local SGLang-Omni.

The worker is designed for rented GPU machines. It opens an outbound WebSocket to the manager, so the GPU host does not need a stable public inbound address.

## Run With Docker

```bash
cp .env.example .env
# edit MANAGER_URL and WORKER_TOKEN
docker compose up --build
```

By default, Docker Compose mounts `./models:/models` and `./cache:/cache`. Override `HOST_MODEL_DIR`, `HOST_CACHE_DIR`, `CONTAINER_MODEL_DIR`, `CONTAINER_CACHE_DIR`, `MODEL_DIR`, and `CACHE_DIR` in `.env` if your 4090 host uses different storage paths. The container downloads `fishaudio/s2-pro` to `MODEL_DIR` if the model directory is incomplete, generates a S2-Pro config from `configs/s2pro_tts.yaml` with the same conservative 4090D runtime defaults as bare-metal setup, starts SGLang-Omni, waits for health, and connects to the manager.

The Docker default is tuned for 24GB GPUs: `SGLANG_TTS_MEM_FRACTION_STATIC=0.45`, `SGLANG_TTS_MAX_NEW_TOKENS=512`, single inflight request, no worker queue, torch compile off, and CUDA graph off. On 32GB GPUs, raise the settings using the tuning guide below.

## Run With uv

```bash
cp .env.example .env
set -a
source .env
set +a
uv sync
bash scripts/start.sh
```

Do not run this on a non-GPU machine unless `WORKER_MANAGE_SGLANG=0` and a compatible SGLang server is already available at `SGLANG_HOST:SGLANG_PORT`.

## AutoDL Bare Metal

Use this on an AutoDL instance created from the PyTorch 2.8 / CUDA 12.8 image. Clone without history, then run setup from the repository root:

```bash
REPO_URL="https://github.com/rikaaa0928/fish-speech.git"
REPO_REF="rev-agent"
git clone --depth 1 --branch "${REPO_REF}" "${REPO_URL}" fish-speech
cd fish-speech
MANAGER_URL=wss://your-manager.example.com/internal/workers/ws \
WORKER_TOKEN=replace-me \
bash scripts/autodl_setup.sh
```

Start the worker after setup:

```bash
bash scripts/autodl_start_worker.sh
```

The setup script prepares the worker only. It installs `uv`, creates `.venv`, reuses the AutoDL image's PyTorch CUDA environment when available, installs PyTorch `2.8.0+cu128` only when missing or incompatible, installs SGLang-Omni, writes `fish-worker/.env`, downloads `hfd.sh`, and downloads `fishaudio/s2-pro` with `hfd.sh` to `/autodl-fs/data/models/s2-pro` by default. Override the model load/download path with `MODEL_DIR=/path/to/model`.

SGLang-Omni can pin a newer CUDA 12.8 PyTorch release than the base AutoDL image. The setup check accepts worker venvs with CUDA-available PyTorch `2.8.x` or `2.9.x`; the tested SGLang-Omni checkout currently installs `torch==2.9.1`.

The tested 4090D base image had no `python3` on `PATH`; `scripts/autodl_setup.sh` selects `/root/miniconda3/bin/python` automatically when present. It also applies a default uv override, `SGLANG_OMNI_UV_OVERRIDES=protobuf>=6.31.1,<7.0.0`, for the current SGLang-Omni dependency resolver conflict.

Set `INSTALL_TORCH=1` to force reinstall PyTorch, or `INSTALL_TORCH=0` to skip PyTorch installation and only validate the existing environment.

Python dependencies use the Tsinghua PyPI mirror by default: `PYPI_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple`. PyTorch CUDA wheels use the Aliyun PyTorch wheel mirror by default: `PYTORCH_INDEX_URL=https://mirrors.aliyun.com/pytorch-wheels/cu128`. `uv python install` uses `UV_PYTHON_INSTALL_MIRROR=https://mirrors.tuna.tsinghua.edu.cn/github-release/astral-sh/python-build-standalone`. Override these if needed.

`scripts/autodl_setup.sh` prepares the standard worker environment only. It intentionally does not prepare `fish-manager`. Rust is installed by `scripts/autodl_start_manager.sh` only when you need to run a local manager for testing.

If you copied only the root `scripts/autodl_setup.sh` to a fresh machine, set `REPO_URL` and optionally `REPO_REF` so it can clone the project first.

For GPU-size-specific settings and tuning recipes, see [`AUTODL_TUNING.md`](AUTODL_TUNING.md).

## Important Environment

- `MANAGER_URL`: manager worker WebSocket endpoint, for example `wss://example.com/internal/workers/ws`.
- `WORKER_TOKEN`: worker registration token. This is not the public OpenAI API key.
- `HOST_MODEL_DIR` and `HOST_CACHE_DIR`: Docker host bind-mount paths, defaults `./models` and `./cache`.
- `CONTAINER_MODEL_DIR` and `CONTAINER_CACHE_DIR`: Docker container mount targets, defaults `/models` and `/cache`.
- `MODEL_ID`: Hugging Face model ID, default `fishaudio/s2-pro`.
- `MODEL_DIR`: local model directory, Docker default `/models/s2-pro`; AutoDL setup writes `/autodl-fs/data/models/s2-pro`.
- `HFD_SCRIPT`: `hfd.sh` path, Docker default `/cache/hfd.sh`; AutoDL setup writes `/autodl-fs/data/hfd.sh`.
- `HFD_TOOL`: `hfd.sh` downloader, default `aria2c`.
- `HFD_THREADS`: download connections, default `8`.
- `SGLANG_MAX_RUNNING_REQUESTS`: SGLang concurrency limit.
- `SGLANG_MAX_QUEUED_REQUESTS`: SGLang queue limit.
- `SGLANG_TTS_MAX_NEW_TOKENS`: S2-Pro TTS engine output-token limit, default `512`.
- `WORKER_MAX_INFLIGHT`: worker-side local inflight limit.
- `WORKER_MAX_QUEUE`: worker-side local queue allowance.

The default S2-Pro settings are conservative for 24GB GPUs. On 32GB GPUs, try `SGLANG_MAX_RUNNING_REQUESTS=1`, `SGLANG_MAX_QUEUED_REQUESTS=0`, `WORKER_MAX_INFLIGHT=1`, `WORKER_MAX_QUEUE=0`, `SGLANG_TTS_MEM_FRACTION_STATIC=0.65`, `SGLANG_TTS_MAX_NEW_TOKENS=1024`, `SGLANG_TTS_TORCH_COMPILE=0`, and `SGLANG_TTS_CUDA_GRAPH=0`, then tune per GPU.
