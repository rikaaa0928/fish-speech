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
