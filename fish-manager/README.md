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

- `GET /health`
- `GET /v1/workers`
- `POST /v1/audio/speech`
- `POST /v1/tts`
- `POST /v1/voices`
- `GET /v1/voices`
- `GET /v1/voices/{voice_id}`
- `DELETE /v1/voices/{voice_id}`

## Create Voice

```bash
curl -X POST http://127.0.0.1:8080/v1/voices \
  -H 'Authorization: Bearer sk-live-1' \
  -H 'Content-Type: application/json' \
  -d '{
    "voice_id": "speaker_a",
    "text": "reference transcript",
    "content_type": "audio/wav",
    "audio_base64": "..."
  }'
```

## Synthesize

```bash
curl -X POST http://127.0.0.1:8080/v1/audio/speech \
  -H 'Authorization: Bearer sk-live-1' \
  -H 'Content-Type: application/json' \
  -o output.wav \
  -d '{
    "input": "Hello from fish-manager",
    "voice_id": "speaker_a",
    "response_format": "wav"
  }'
```
