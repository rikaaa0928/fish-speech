# fish-worker

Outbound GPU worker bridge for `fish-manager` and local SGLang-Omni.

The worker is designed for rented GPU machines. It opens an outbound WebSocket to the manager, so the GPU host does not need a stable public inbound address.

## Run With Docker

```bash
cp .env.example .env
# edit MANAGER_URL and WORKER_TOKEN
docker compose up --build
```

The container downloads `fishaudio/s2-pro` into `./models` if the model directory is empty, starts SGLang-Omni, waits for health, and connects to the manager.

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
git clone --depth 1 https://github.com/fishaudio/fish-speech.git
cd fish-speech
MANAGER_URL=wss://your-manager.example.com/internal/workers/ws \
WORKER_TOKEN=replace-me \
bash scripts/autodl_setup.sh
```

Start the worker after setup:

```bash
bash scripts/autodl_start_worker.sh
```

The setup script installs `uv`, creates `.venv`, reuses the AutoDL image's PyTorch `2.8.x` / CUDA `12.8` when available, installs PyTorch `2.8.0+cu128` only when missing or incompatible, installs SGLang-Omni, writes `.env`, and downloads `fishaudio/s2-pro` to `/root/autodl-fs/models/s2-pro` by default. Override the model load/download path with `MODEL_DIR=/path/to/model`.

Set `INSTALL_TORCH=1` to force reinstall PyTorch, or `INSTALL_TORCH=0` to skip PyTorch installation and only validate the existing environment.

Python dependencies use the Tsinghua PyPI mirror by default: `PYPI_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple`. PyTorch CUDA wheels use the Aliyun PyTorch wheel mirror by default: `PYTORCH_INDEX_URL=https://mirrors.aliyun.com/pytorch-wheels/cu128`. `uv python install` uses `UV_PYTHON_INSTALL_MIRROR=https://mirrors.tuna.tsinghua.edu.cn/github-release/astral-sh/python-build-standalone`. Override these if needed.

Rust setup uses domestic mirrors by default: rustup downloads from `RUSTUP_DIST_SERVER=https://mirrors.ustc.edu.cn/rust-static`, and Cargo crates use `CARGO_REGISTRY_URL=sparse+https://mirrors.tuna.tsinghua.edu.cn/crates.io-index/`.

If you copied only the root `scripts/autodl_setup.sh` to a fresh machine, set `REPO_URL` and optionally `REPO_REF` so it can clone the project first.

## Important Environment

- `MANAGER_URL`: manager worker WebSocket endpoint, for example `wss://example.com/internal/workers/ws`.
- `WORKER_TOKEN`: worker registration token. This is not the public OpenAI API key.
- `MODEL_ID`: Hugging Face model ID, default `fishaudio/s2-pro`.
- `MODEL_DIR`: local model directory.
- `SGLANG_MAX_RUNNING_REQUESTS`: SGLang concurrency limit.
- `SGLANG_MAX_QUEUED_REQUESTS`: SGLang queue limit.
- `WORKER_MAX_INFLIGHT`: worker-side local inflight limit.
- `WORKER_MAX_QUEUE`: worker-side local queue allowance.

For realtime behavior, start with `SGLANG_MAX_RUNNING_REQUESTS=2`, `SGLANG_MAX_QUEUED_REQUESTS=0`, `WORKER_MAX_INFLIGHT=2`, and `WORKER_MAX_QUEUE=0`, then tune per GPU.
