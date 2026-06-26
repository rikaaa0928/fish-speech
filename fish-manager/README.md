# fish-manager

Rust manager for the dynamic Fish Speech/SGLang TTS platform.

## Run

```bash
cp .env.example .env
set -a
source .env
set +a
cargo run
```

## AutoDL Bare Metal

Clone without history, then initialize the standard worker environment from the repository root:

```bash
REPO_URL="https://github.com/rikaaa0928/fish-speech.git"
REPO_REF="rev-agent"
git clone --depth 1 --branch "${REPO_REF}" "${REPO_URL}" fish-speech
cd fish-speech
bash scripts/autodl_setup.sh
```

Build and start `fish-manager` on AutoDL for local testing:

```bash
bash scripts/autodl_start_manager.sh
```

From the repository root, you can also run the `fish-manager` wrapper:

```bash
cd fish-manager
bash scripts/autodl_start.sh
```

The shared setup script prepares the worker environment only. The manager script installs Rust when missing, creates `fish-manager/.env` when missing, builds the Rust release binary, and starts it with data under `/root/autodl-fs/fish-manager-data` by default.

Useful overrides:

- `MANAGER_BIND_ADDR=0.0.0.0:8080`
- `OPENAI_API_KEYS=sk-live-1,sk-live-2`
- `WORKER_TOKEN=replace-me`
- `MAX_REQUEST_BODY_BYTES=67108864`
- `AUTODL_FS=/root/autodl-fs`
- `MANAGER_PROFILE=debug`
- `CARGO_REGISTRY_URL=sparse+https://mirrors.tuna.tsinghua.edu.cn/crates.io-index/`
- `RUSTUP_DIST_SERVER=https://mirrors.ustc.edu.cn/rust-static`

Public APIs use OpenAI-compatible authentication:

```http
Authorization: Bearer sk-live-1
```

Workers authenticate separately with `WORKER_TOKEN` and connect to:

```text
wss://your-manager.example.com/internal/workers/ws
```

For local development without TLS, use:

```text
ws://127.0.0.1:8080/internal/workers/ws
```

## Public Endpoints

See [API.md](API.md) for the standalone API calling guide with request fields and curl examples.

- `GET /health`
- `GET /v1/workers`
- `POST /v1/audio/speech`
- `POST /v1/tts`
- `POST /v1/references/add`
- `GET /v1/references/list`
- `DELETE|POST /v1/references/delete`
- `POST /v1/references/update`

## API Availability Check

Use the uv script to check whether manager APIs are reachable:

```bash
uv run --script scripts/check_apis.py
```

The script reads `.env` by default and uses `OPENAI_API_KEY`, or the first value from `OPENAI_API_KEYS`. You can also pass values explicitly:

```bash
uv run --script scripts/check_apis.py \
  --base-url http://127.0.0.1:8080 \
  --api-key sk-live-1 \
  --worker-token replace-me
```

Run only one or several checks with `--only`. The option can be repeated or comma-separated:

```bash
uv run --script scripts/check_apis.py --only health
uv run --script scripts/check_apis.py --only health --only workers
uv run --script scripts/check_apis.py --only health,workers,tts
```

Common `--only` values:

- `health`
- `workers`
- `references`
- `references-list`
- `references-add`
- `references-update`
- `references-delete`
- `speech`
- `audio-speech`
- `tts`
- `worker-ws`
- `public`
- `all`

Endpoint-style aliases are also supported, for example:

```bash
uv run --script scripts/check_apis.py --only "POST /v1/tts"
```

By default, TTS checks treat `HTTP 429` as a warning because it means the API is reachable but no healthy worker is available. To require a real worker audio response, add `--require-worker`:

```bash
uv run --script scripts/check_apis.py --only speech --require-worker
```

The script prints response bodies for non-binary responses. Successful `/v1/audio/speech` and `/v1/tts` binary audio responses print only the content type and byte count.

## Add Reference

```bash
curl -X POST http://127.0.0.1:8080/v1/references/add \
  -H 'Authorization: Bearer sk-live-1' \
  -F 'id=speaker_a' \
  -F 'text=reference transcript' \
  -F 'audio=@reference.wav;type=audio/wav'
```

## Synthesize

```bash
curl -X POST http://127.0.0.1:8080/v1/audio/speech \
  -H 'Authorization: Bearer sk-live-1' \
  -H 'Content-Type: application/json' \
  -o output.wav \
  -d '{
    "input": "Hello from fish-manager",
    "voice": "speaker_a",
    "response_format": "wav"
  }'
```

To pin a request to one connected worker and disable cross-worker fallback for that request, add `X-Fish-Worker-ID`:

```bash
curl -X POST http://127.0.0.1:8080/v1/audio/speech \
  -H 'Authorization: Bearer sk-live-1' \
  -H 'Content-Type: application/json' \
  -H 'X-Fish-Worker-ID: worker-a' \
  -o output.wav \
  -d '{
    "input": "Hello from fish-manager",
    "voice": "speaker_a",
    "response_format": "wav"
  }'
```
