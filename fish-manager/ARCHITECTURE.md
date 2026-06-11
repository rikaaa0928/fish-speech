# Fish Speech Dynamic TTS Platform Architecture

## Goals

Build a dynamically scalable voice clone and TTS platform on top of Fish Speech and SGLang-Omni.

The platform must support GPU rental workers that do not have stable public inbound access. Workers connect to a public manager, register themselves, keep a long-lived connection, and receive inference requests through that connection.

## High-Level Architecture

```text
Client
  |
  | HTTP/SSE
  v
fish-manager
  |  Rust, public API, auth, scheduling, voice storage
  ^
  | WebSocket over TLS, worker initiated
  v
fish-worker
  |  Python bridge, local overload guard, model bootstrap
  |
  | localhost HTTP
  v
SGLang-Omni
  |  sgl-omni serve --model-path /models/s2-pro
  v
GPU inference
```

## Component Responsibilities

### fish-manager

The manager is the only public service in the MVP.

Responsibilities:

- Expose public TTS APIs.
- Authenticate external clients with OpenAI-compatible bearer tokens.
- Accept worker registrations over a worker-initiated long connection.
- Track worker health, load, capacity, model version, and GPU metadata.
- Route requests to available workers.
- Return `429 Too Many Requests` when all workers are saturated.
- Store voice clone metadata and reference audio through a modular storage layer.
- Provide health, worker list, and metrics endpoints.

The first implementation is single-instance. Multi-instance support is intentionally deferred, with TODO markers added near storage, worker registry, and request ownership boundaries.

### fish-worker

The worker runs on rented GPU machines.

Responsibilities:

- Check whether the S2-Pro model exists locally.
- Download missing model files from Hugging Face mirror.
- Start a local SGLang-Omni server.
- Wait for SGLang health readiness.
- Connect to the manager by outbound WebSocket.
- Register GPU and capacity information.
- Receive inference requests from the manager.
- Write request-scoped reference audio to local files for SGLang.
- Forward requests to local SGLang `/v1/audio/speech`.
- Stream audio chunks back to the manager.
- Reject locally when worker pressure exceeds configured limits.

## Public API

The primary public API is SGLang/OpenAI-style.

All public endpoints use OpenAI-compatible authentication:

```http
Authorization: Bearer sk-...
```

This keeps the manager compatible with OpenAI-style SDKs, reverse proxies, and client conventions. The public API token is separate from the worker registration token.

Recommended manager configuration:

```env
OPENAI_API_KEYS=sk-live-1,sk-live-2
```

Requests without a valid bearer token return:

```http
HTTP/1.1 401 Unauthorized
WWW-Authenticate: Bearer
Content-Type: application/json
```

```json
{
  "error": {
    "message": "Invalid authentication credentials",
    "type": "invalid_request_error",
    "code": "invalid_api_key"
  }
}
```

### `POST /v1/audio/speech`

Main endpoint.

Example:

```json
{
  "input": "Hello, how are you?",
  "voice_id": "speaker_a",
  "response_format": "wav",
  "stream": false,
  "temperature": 0.8,
  "top_p": 0.8,
  "repetition_penalty": 1.1,
  "max_new_tokens": 1024,
  "seed": 123
}
```

Supported request concepts:

- `input`: text to synthesize.
- `voice_id`: manager-side saved voice clone ID.
- `references`: direct reference audio objects for one-off cloning.
- `response_format`: `wav`, `mp3`, `flac`, `opus`, `aac`, or `pcm`, subject to SGLang support.
- `stream`: stream response through SSE or raw audio mode.
- Sampling parameters: `temperature`, `top_p`, `top_k`, `repetition_penalty`, `max_new_tokens`, `seed`.

### `POST /v1/tts`

Fish Speech compatibility endpoint.

This endpoint is supported but internally converted to `/v1/audio/speech` semantics.

Mapping:

```text
text -> input
format -> response_format
streaming -> stream
reference_id -> voice_id
references -> references
top_p -> top_p
temperature -> temperature
repetition_penalty -> repetition_penalty
max_new_tokens -> max_new_tokens
seed -> seed
```

### Reference APIs

Initial manager endpoints:

- `POST /v1/references/add`
- `GET /v1/references/list`
- `DELETE /v1/references/delete`
- `POST /v1/references/update`

Reference creation stores:

- reference ID
- reference text
- reference audio blob or object path
- content type
- size and duration if available
- checksum
- created time

## SGLang Reference Audio Handling

SGLang-Omni `references[].audio_path` supports a local audio file path or URL. The design must treat it as a file path, not a directory path.

Valid SGLang request shape:

```json
{
  "input": "Text to synthesize",
  "references": [
    {
      "audio_path": "/cache/refs/req_123.wav",
      "text": "Transcript of the reference audio"
    }
  ],
  "response_format": "wav"
}
```

The manager does not pass its local filesystem paths to workers. Instead:

1. Client sends `voice_id` or direct reference audio.
2. Manager resolves reference metadata and audio bytes through `VoiceStore`.
3. Manager sends reference audio bytes and text to worker inside the internal request.
4. Worker writes the audio to a local temporary/cache file.
5. Worker calls SGLang with `references[].audio_path` pointing to that local file.

This avoids relying on worker access to manager disk, public URLs, or shared volumes.

## Worker-Manager Transport

Use WebSocket over TLS for MVP.

Rationale:

- GPU rental machines only need outbound connectivity.
- WebSocket works reliably through common reverse proxies.
- Binary audio chunks are easy to stream.
- Rust and Python implementations are simple.
- Operational debugging is easier than bidirectional gRPC in mixed network environments.

Protocol payloads should use MessagePack for binary friendliness.

gRPC bidirectional streaming remains a possible future transport, but is not used in the MVP because HTTP/2 support through rental platform networking and proxies is less predictable.

## Internal Protocol

Worker connects to:

```text
wss://manager.example.com/internal/workers/ws?token=...
```

Authentication:

- `WORKER_TOKEN` for worker registration.
- Public API auth uses OpenAI-compatible `Authorization: Bearer <token>` and is separate from worker auth.

Message types:

```text
WorkerHello
Heartbeat
InferenceRequest
InferenceChunk
InferenceDone
InferenceError
CancelRequest
```

### WorkerHello

Sent once after connecting.

Fields:

- `worker_id`
- `version`
- `model_id`
- `model_revision`
- `gpu_name`
- `gpu_count`
- `vram_total_mb`
- `max_running_requests`
- `max_queued_requests`
- `worker_max_inflight`
- `worker_max_queue`
- `sglang_url`
- `started_at`

### Heartbeat

Sent periodically.

Fields:

- `worker_id`
- `ready`
- `sglang_healthy`
- `inflight`
- `queued`
- `max_running_requests`
- `max_queued_requests`
- `vram_used_mb`
- `vram_free_mb`
- `gpu_utilization_percent`
- `ewma_latency_ms`
- `last_error`

### InferenceRequest

Sent by manager to worker.

Fields:

- `request_id`
- `api_kind`: `audio_speech` or `fish_tts`
- `payload`: normalized SGLang-style payload
- `references`: optional list of `{ audio_bytes, content_type, text }`
- `stream`
- `deadline_ms`

### InferenceChunk

Sent by worker to manager.

Fields:

- `request_id`
- `seq`
- `content_type`
- `bytes`
- `is_sse`

### InferenceError

Fields:

- `request_id`
- `code`
- `message`
- `retryable`

Important error codes:

- `overloaded`
- `sglang_unavailable`
- `model_not_ready`
- `bad_request`
- `inference_failed`
- `deadline_exceeded`
- `cancelled`

## Scheduling And Overload Strategy

The platform prioritizes realtime behavior over unlimited queueing.

SGLang has its own scheduler, running batch, waiting queue, and rejection limit. The platform still applies overload control before requests reach SGLang.

### SGLang Limits

Worker should start SGLang with explicit limits:

```bash
sgl-omni serve \
  --model-path /models/s2-pro \
  --config /app/configs/s2pro_tts.yaml \
  --host 127.0.0.1 \
  --port 8000 \
  --max-running-requests "${SGLANG_MAX_RUNNING_REQUESTS}" \
  --max-queued-requests "${SGLANG_MAX_QUEUED_REQUESTS}"
```

`--max-running-requests` should be configured based on GPU memory, model, average request length, reference audio usage, and realtime SLA. SGLang is memory-aware, but the platform should not rely on it as the only realtime guard.

Recommended initial profiles:

```text
Realtime strict:
  SGLANG_MAX_RUNNING_REQUESTS=2
  SGLANG_MAX_QUEUED_REQUESTS=0

Balanced:
  SGLANG_MAX_RUNNING_REQUESTS=4
  SGLANG_MAX_QUEUED_REQUESTS=2

High-memory throughput:
  SGLANG_MAX_RUNNING_REQUESTS=8
  SGLANG_MAX_QUEUED_REQUESTS=4
```

These are starting points only and must be tuned per GPU type.

### Manager Admission Control

The manager only schedules to workers that are ready and below pressure thresholds.

A worker is unavailable for new requests when:

```text
ready == false
sglang_healthy == false
inflight + queued >= worker_max_inflight + worker_max_queue
```

If no worker can accept the request, manager returns:

```http
HTTP/1.1 429 Too Many Requests
Retry-After: 2
Content-Type: application/json
```

```json
{
  "error": {
    "code": "tts_overloaded",
    "message": "All workers are busy. Please retry later."
  }
}
```

For realtime mode, `MANAGER_QUEUE_TIMEOUT_MS` defaults to `0`, meaning the manager does not hold a cross-worker queue.

### Worker Forwarding

The worker does not keep a separate local admission queue. It forwards accepted manager requests to SGLang and maps SGLang `429` responses to `InferenceError(code="overloaded", retryable=true)`.

Manager behavior on retryable worker overload:

- If response has not started, retry remaining eligible workers.
- If no eligible worker exists, return `429`.
- If streaming output already started, do not retry on another worker.

### SGLang As Final Guard

If SGLang still rejects due to its own `max_queued_requests`, worker maps that response to:

```text
InferenceError(code="overloaded", retryable=true)
```

## Storage Architecture

Manager storage must be modular because future deployments may replace local disk and SQLite with cloud/object/distributed systems.

### Storage Traits

The manager should depend on traits/interfaces rather than concrete storage implementations.

Recommended Rust modules:

```text
src/storage/mod.rs
src/storage/local.rs
src/storage/sqlite.rs
src/storage/postgres.rs      # TODO(ha)
src/storage/s3.rs            # TODO(ha)
src/voice_store.rs
```

Core interfaces:

```rust
#[async_trait]
pub trait VoiceMetadataStore: Send + Sync {
    async fn create_voice(&self, voice: VoiceMetadata) -> Result<()>;
    async fn get_voice(&self, voice_id: &str) -> Result<Option<VoiceMetadata>>;
    async fn list_voices(&self, cursor: Option<String>, limit: u32) -> Result<VoicePage>;
    async fn delete_voice(&self, voice_id: &str) -> Result<()>;
}

#[async_trait]
pub trait VoiceBlobStore: Send + Sync {
    async fn put_audio(&self, key: &str, bytes: Bytes, content_type: &str) -> Result<BlobInfo>;
    async fn get_audio(&self, key: &str) -> Result<Bytes>;
    async fn delete_audio(&self, key: &str) -> Result<()>;
}

#[async_trait]
pub trait StorageHealth: Send + Sync {
    async fn health_check(&self) -> Result<()>;
}
```

`VoiceStore` composes metadata and blob stores:

```text
VoiceStore
  - metadata: dyn VoiceMetadataStore
  - blobs: dyn VoiceBlobStore
```

### MVP Storage Backend

MVP backend:

- Metadata: SQLite.
- Audio blobs: local filesystem.

Suggested layout:

```text
fish-manager-data/
  manager.sqlite3
  voices/
    ab/
      voice_id_checksum.wav
```

Configuration:

```env
STORAGE_METADATA_BACKEND=sqlite
STORAGE_BLOB_BACKEND=local
SQLITE_PATH=/data/manager.sqlite3
BLOB_LOCAL_DIR=/data/voices
```

### Future Storage Backends

Future replacements should not affect API, scheduler, or worker protocol.

TODO markers to add during implementation:

```rust
// TODO(ha): replace local SQLite metadata with Postgres for multi-manager deployments.
// TODO(ha): replace local blob storage with S3/MinIO for multi-manager deployments.
// TODO(ha): add transactional cleanup between metadata and blob stores.
// TODO(ha): add object versioning or reference counting for shared voice assets.
```

## Single-Instance Manager Design

Initial manager is single-instance.

In-memory state:

- Worker registry.
- Active WebSocket connections.
- In-flight request ownership.
- Scheduler load snapshot.

Persistent state:

- Voice metadata.
- Voice audio blobs.

TODO markers for future HA:

```rust
// TODO(ha): move worker registry to Redis, NATS, or another shared coordination layer.
// TODO(ha): move in-flight request ownership to a distributed lease model.
// TODO(ha): add manager instance ID and worker sticky registration.
// TODO(ha): add cross-manager request cancellation routing.
// TODO(ha): add shared metrics and load snapshots for multiple managers.
```

## Worker Startup Flow

```text
1. Validate CUDA and GPU availability.
2. Set HF_ENDPOINT, default https://hf-mirror.com.
3. Check MODEL_DIR for model files.
4. If missing, run hf download fishaudio/s2-pro --local-dir MODEL_DIR.
5. Start SGLang-Omni on 127.0.0.1:SGLANG_PORT.
6. Wait for SGLang health endpoint.
7. Connect to manager WebSocket.
8. Send WorkerHello.
9. Start heartbeat loop.
10. Accept inference requests.
```

Worker environment:

```env
MANAGER_URL=wss://manager.example.com/internal/workers/ws
WORKER_TOKEN=replace-me
WORKER_ID=
HF_ENDPOINT=https://hf-mirror.com
MODEL_ID=fishaudio/s2-pro
MODEL_DIR=/models/s2-pro
SGLANG_PORT=8000
SGLANG_MAX_RUNNING_REQUESTS=4
SGLANG_MAX_QUEUED_REQUESTS=2
```

## Docker Strategy

Worker should use the official SGLang-Omni image initially to avoid CUDA, UCX, and flash-attn compatibility issues.

Recommended base:

```dockerfile
FROM lmsysorg/sglang-omni:dev
```

Later production hardening should pin an immutable digest or build a controlled image.

Worker compose should use:

```yaml
services:
  fish-worker:
    build: .
    restart: unless-stopped
    shm_size: "32g"
    ipc: host
    gpus: all
    environment:
      MANAGER_URL: "wss://manager.example.com/internal/workers/ws"
      WORKER_TOKEN: "replace-me"
      HF_ENDPOINT: "https://hf-mirror.com"
      MODEL_ID: "fishaudio/s2-pro"
      MODEL_DIR: "/models/s2-pro"
      SGLANG_MAX_RUNNING_REQUESTS: "4"
      SGLANG_MAX_QUEUED_REQUESTS: "2"
    volumes:
      - ./models:/models
      - ./cache:/cache
```

## Security

Security requirements:

- Public API uses OpenAI-compatible `Authorization: Bearer <token>`.
- Worker registration uses separate worker token.
- Worker transport uses TLS, `wss://` only in production.
- Uploaded reference audio has max size and duration limits.
- Manager validates content type and decodes audio when possible.
- Worker should not fetch arbitrary user-provided URLs directly.
- Manager should resolve external reference URLs, validate them, and pass bytes to worker.
- Request logs must not include raw audio bytes or secrets.

## Observability

Manager metrics:

- online workers
- ready workers
- request count
- request latency
- request rejection count
- 429 count
- worker overload count
- worker disconnect count
- voice storage errors

Worker metrics in heartbeat:

- SGLang health
- inflight
- queued
- request latency EWMA
- GPU utilization
- VRAM used and free
- local reject count
- SGLang reject count

Logs should include:

- `request_id`
- `worker_id`
- `voice_id`
- `model_id`
- overload/retry decisions

## Implementation Order

1. Create `fish-manager` Rust project with config, health endpoint, and worker WebSocket endpoint.
2. Add internal MessagePack protocol types.
3. Implement in-memory worker registry and single-instance scheduler.
4. Implement modular storage traits and local SQLite/filesystem backend.
5. Implement `/v1/audio/speech` non-streaming path.
6. Implement `fish-worker` bootstrap, model download, SGLang startup, and manager registration.
7. Add worker request forwarding to local SGLang.
8. Add streaming relay.
9. Add `/v1/tts` compatibility endpoint.
10. Add voice APIs and manager-side voice store integration.
11. Add Dockerfile, compose, env examples, and README files.
12. Add load tests for overload and 429 behavior.

## Open Decisions

No blocking decisions remain for MVP.

Parameters that should be tuned during implementation and testing:

- Default `SGLANG_MAX_RUNNING_REQUESTS` by GPU type.
- Default `SGLANG_MAX_QUEUED_REQUESTS` for realtime vs balanced mode.
- Max reference audio size and duration.
- Public API auth format.
- Whether streaming response defaults to SSE or raw audio for `/v1/audio/speech`.
