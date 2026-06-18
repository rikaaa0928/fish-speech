# fish-worker

Outbound GPU worker bridge for `fish-manager` and a local Fish API server.

The worker is designed for rented GPU machines. It opens an outbound WebSocket to the manager, so the GPU host does not need a stable public inbound address.

## Run With Docker

```bash
cp .env.example .env
# edit MANAGER_URL and WORKER_TOKEN
docker compose up --build
```

Docker Compose mounts `./models:/models` and `./cache:/cache`, starts Fish API server on `0.0.0.0:${API_SERVER_PORT:-8000}`, exposes it on host port `9281`, waits for `/v1/health`, and connects the worker to the manager.

The container downloads `fishaudio/s2-pro` to `MODEL_DIR` if the model directory is incomplete. Override `HOST_MODEL_DIR`, `HOST_CACHE_DIR`, `CONTAINER_MODEL_DIR`, `CONTAINER_CACHE_DIR`, `MODEL_DIR`, and `CACHE_DIR` in `.env` if your GPU host uses different storage paths.

## Run With uv

From the repository root:

```bash
cd fish-worker
cp .env.example .env
# edit MANAGER_URL and WORKER_TOKEN
set -a
source .env
set +a
cd ..
UV_PROJECT_ENVIRONMENT=fish-worker/.venv uv sync --extra cu128 --no-dev
uv pip install --python fish-worker/.venv/bin/python -e fish-worker
bash fish-worker/scripts/start.sh
```

Do not run this on a non-GPU machine unless `WORKER_MANAGE_API_SERVER=0` and a compatible Fish API server is already available at `API_SERVER_URL` or `API_SERVER_HOST:API_SERVER_PORT`.

## AutoDL Bare Metal

Use this on an AutoDL instance with CUDA 12.8-compatible PyTorch wheels. Clone without history, then run setup from the repository root:

```bash
REPO_URL="https://github.com/rikaaa0928/fish-speech.git"
REPO_REF="worker-api-server-backend"
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

The setup script installs `uv`, creates `fish-worker/.venv`, installs the root Fish Speech API server dependencies and the worker package into that venv, writes `fish-worker/.env`, downloads `hfd.sh` to `/root/autodl-tmp/cache/hfd.sh`, and downloads `fishaudio/s2-pro` to `/autodl-fs/data/models/s2-pro` by default.

Python dependencies use the Aliyun PyPI mirror by default: `PYPI_INDEX_URL=https://mirrors.aliyun.com/pypi/simple`. `uv python install` uses `UV_PYTHON_INSTALL_MIRROR=https://mirrors.tuna.tsinghua.edu.cn/github-release/astral-sh/python-build-standalone`. Override these if needed.

For GPU-size-specific settings and tuning recipes, see [`AUTODL_TUNING.md`](AUTODL_TUNING.md).

## Important Environment

- `MANAGER_URL`: manager worker WebSocket endpoint, for example `wss://example.com/internal/workers/ws`.
- `WORKER_TOKEN`: worker registration token. This is not the public OpenAI API key.
- `MODEL_ID`: Hugging Face model ID, default `fishaudio/s2-pro`.
- `MODEL_DIR`: local model directory, Docker default `/models/s2-pro`; AutoDL setup writes `/autodl-fs/data/models/s2-pro`.
- `CACHE_DIR`: runtime cache directory, Docker default `/cache`; AutoDL setup writes `/root/autodl-tmp/cache`.
- `API_SERVER_HOST` and `API_SERVER_PORT`: managed Fish API server listen address, defaults `0.0.0.0:8000` in Docker and `127.0.0.1:8000` in AutoDL setup.
- `API_SERVER_URL`: optional worker target URL for an external Fish API server. Leave empty when the worker manages the API server.
- `WORKER_MANAGE_API_SERVER`: set `0` to connect to an external server instead of starting `tools/api_server.py`.
- `API_SERVER_MAX_RUNNING_REQUESTS`: worker local running request limit. This is enforced before requests reach the API server.
- `API_SERVER_MAX_QUEUED_REQUESTS`: worker local queue limit, default `1`. Set `0` to reject overload immediately.
- `API_SERVER_TTS_MAX_NEW_TOKENS`: default output-token limit when a client request omits `max_new_tokens`.
- `API_SERVER_COMPILE`: starts Fish API server with `--compile`, default `1`.
- `API_SERVER_HALF`: starts Fish API server with `--half`, default `0`; default bf16 was more stable in testing.
- `API_SERVER_WORKERS`: Uvicorn worker count, default `1`. Each worker loads its own model copy.
- `API_SERVER_MAX_TEXT_LENGTH`: Fish API server text length guard, default `0` disabled. Use around `240` on 24GB GPUs if you want a conservative guard.
- `API_SERVER_REFERENCES_DIR`: directory that Fish API server reads as `references/`, default `references` relative to the repository root. Docker mounts this at `/app/references`.
- `PYTORCH_ALLOC_CONF`: PyTorch CUDA allocator setting, default `expandable_segments:True`.

When `API_SERVER_MAX_RUNNING_REQUESTS` and `API_SERVER_MAX_QUEUED_REQUESTS` are exceeded, the worker returns retryable `overloaded` to the manager instead of sending the request to the local API server.

Stored manager voices are pulled on demand. The worker writes them directly in Fish API server's on-disk format: `references/{reference_id}/sample.<ext>` and `references/{reference_id}/sample.lab`. For a request with one stored `voice_id/checksum` reference, `/v1/tts` receives only `reference_id`. Inline client references without a stable stored voice ID are still forwarded as request-local audio bytes.
