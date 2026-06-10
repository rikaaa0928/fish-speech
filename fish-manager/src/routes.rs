use std::{collections::HashSet, sync::atomic::Ordering};

use async_stream::stream;
use axum::{
    body::Body,
    extract::{Path, Query, State},
    http::{header, HeaderMap, HeaderValue, StatusCode},
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use base64::{engine::general_purpose::STANDARD, Engine as _};
use bytes::{Bytes, BytesMut};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tokio::sync::mpsc;
use uuid::Uuid;

use crate::{
    auth::{require_openai_auth, require_worker_auth},
    error::{AppError, AppResult},
    protocol::{InferenceError, InferenceRequest, InternalReference, WireMessage},
    state::{AppState, PendingRequest, WorkerEvent},
    workers::worker_ws_handler,
};

pub fn build_router(state: AppState) -> Router {
    Router::new()
        .route("/health", get(health))
        .route("/v1/workers", get(list_workers))
        .route("/v1/audio/speech", post(audio_speech))
        .route("/v1/tts", post(fish_tts))
        .route("/v1/voices", post(create_voice).get(list_voices))
        .route("/v1/voices/:voice_id", get(get_voice).delete(delete_voice))
        .route(
            "/internal/voices/:voice_id/audio",
            get(get_internal_voice_audio),
        )
        .route("/internal/workers/ws", get(worker_ws_handler))
        .with_state(state)
}

async fn health() -> Json<Value> {
    Json(json!({ "status": "ok" }))
}

async fn list_workers(State(state): State<AppState>, headers: HeaderMap) -> AppResult<Json<Value>> {
    require_openai_auth(&headers, &state.config)?;

    let mut workers = Vec::new();
    for entry in state.workers.iter() {
        let handle = entry.value().clone();
        let mut status = handle.status.read().await.clone();
        status.manager_inflight = handle.manager_inflight.load(Ordering::Relaxed);
        workers.push(status);
    }

    Ok(Json(json!({ "data": workers })))
}

#[derive(Debug, Deserialize)]
struct CreateVoiceRequest {
    voice_id: Option<String>,
    text: String,
    audio_base64: String,
    content_type: Option<String>,
}

async fn create_voice(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(request): Json<CreateVoiceRequest>,
) -> AppResult<Json<Value>> {
    require_openai_auth(&headers, &state.config)?;
    let voice = state
        .voice_store
        .create_voice(
            request.voice_id,
            request.text,
            request.audio_base64,
            request.content_type,
        )
        .await?;
    Ok(Json(json!(voice)))
}

#[derive(Debug, Deserialize)]
struct ListVoicesQuery {
    cursor: Option<String>,
    limit: Option<u32>,
}

async fn list_voices(
    State(state): State<AppState>,
    headers: HeaderMap,
    Query(query): Query<ListVoicesQuery>,
) -> AppResult<Json<Value>> {
    require_openai_auth(&headers, &state.config)?;
    let page = state
        .voice_store
        .list_voices(query.cursor, query.limit.unwrap_or(50))
        .await?;
    Ok(Json(json!(page)))
}

async fn get_voice(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(voice_id): Path<String>,
) -> AppResult<Json<Value>> {
    require_openai_auth(&headers, &state.config)?;
    let voice = state
        .voice_store
        .get_voice(&voice_id)
        .await?
        .ok_or_else(|| AppError::NotFound("voice not found".to_string()))?;
    Ok(Json(json!(voice)))
}

async fn delete_voice(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(voice_id): Path<String>,
) -> AppResult<impl IntoResponse> {
    require_openai_auth(&headers, &state.config)?;
    state.voice_store.delete_voice(&voice_id).await?;
    Ok(StatusCode::NO_CONTENT)
}

#[derive(Debug, Deserialize)]
struct InternalVoiceAudioQuery {
    checksum: Option<String>,
}

async fn get_internal_voice_audio(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(voice_id): Path<String>,
    Query(query): Query<InternalVoiceAudioQuery>,
) -> AppResult<Response> {
    require_worker_auth(&headers, &state.config)?;
    let voice = state
        .voice_store
        .get_voice(&voice_id)
        .await?
        .ok_or_else(|| AppError::NotFound("voice not found".to_string()))?;

    if query
        .checksum
        .as_deref()
        .is_some_and(|checksum| checksum != voice.checksum)
    {
        return Err(AppError::NotFound("voice checksum not found".to_string()));
    }

    let audio = state.voice_store.get_voice_audio(&voice).await?;
    let mut response = binary_response(StatusCode::OK, voice.content_type, audio)?;
    response.headers_mut().insert(
        "x-voice-checksum",
        HeaderValue::from_str(&voice.checksum)
            .map_err(|error| AppError::Internal(anyhow::Error::from(error)))?,
    );
    Ok(response)
}

#[derive(Debug, Clone, Deserialize, Serialize)]
struct AudioSpeechRequest {
    input: String,
    #[serde(default)]
    #[serde(skip_serializing_if = "Option::is_none")]
    voice_id: Option<String>,
    #[serde(default)]
    references: Vec<ClientReference>,
    #[serde(default)]
    #[serde(skip_serializing_if = "Option::is_none")]
    response_format: Option<String>,
    #[serde(default)]
    #[serde(skip_serializing_if = "Option::is_none")]
    stream: Option<bool>,
    #[serde(flatten)]
    extra: serde_json::Map<String, Value>,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
struct ClientReference {
    text: String,
    #[serde(default)]
    audio_base64: Option<String>,
    #[serde(default)]
    audio: Option<String>,
    #[serde(default)]
    content_type: Option<String>,
    #[serde(default)]
    audio_path: Option<String>,
}

#[derive(Debug, Deserialize)]
struct FishTtsRequest {
    text: String,
    #[serde(default)]
    reference_id: Option<String>,
    #[serde(default)]
    references: Vec<ClientReference>,
    #[serde(default)]
    format: Option<String>,
    #[serde(default)]
    streaming: Option<bool>,
    #[serde(flatten)]
    extra: serde_json::Map<String, Value>,
}

async fn audio_speech(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(request): Json<AudioSpeechRequest>,
) -> AppResult<Response> {
    require_openai_auth(&headers, &state.config)?;
    handle_speech(state, request, "audio_speech").await
}

async fn fish_tts(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(request): Json<FishTtsRequest>,
) -> AppResult<Response> {
    require_openai_auth(&headers, &state.config)?;

    let mapped = AudioSpeechRequest {
        input: request.text,
        voice_id: request.reference_id,
        references: request.references,
        response_format: request.format,
        stream: request.streaming,
        extra: request.extra,
    };

    handle_speech(state, mapped, "fish_tts").await
}

async fn handle_speech(
    state: AppState,
    request: AudioSpeechRequest,
    api_kind: &str,
) -> AppResult<Response> {
    if request.input.trim().is_empty() {
        return Err(AppError::BadRequest("input must not be empty".to_string()));
    }

    let stream_response = request.stream.unwrap_or(false);
    let references = resolve_references(&state, &request).await?;
    let payload = normalized_payload(&request)?;

    if stream_response {
        stream_inference(state, payload, references, api_kind.to_string()).await
    } else {
        non_streaming_inference(state, payload, references, api_kind.to_string()).await
    }
}

async fn resolve_references(
    state: &AppState,
    request: &AudioSpeechRequest,
) -> AppResult<Vec<InternalReference>> {
    let mut references = Vec::new();

    if let Some(voice_id) = &request.voice_id {
        let voice = state
            .voice_store
            .get_voice(voice_id)
            .await?
            .ok_or_else(|| AppError::NotFound("voice not found".to_string()))?;
        references.push(InternalReference {
            voice_id: Some(voice.voice_id),
            checksum: Some(voice.checksum),
            audio_bytes: Vec::new(),
            content_type: voice.content_type,
            text: voice.text,
        });
    }

    for reference in &request.references {
        if reference.audio_path.is_some() {
            return Err(AppError::BadRequest(
                "references[].audio_path is not accepted by the manager; send audio_base64 instead"
                    .to_string(),
            ));
        }

        let audio_base64 = reference
            .audio_base64
            .as_deref()
            .or(reference.audio.as_deref())
            .ok_or_else(|| {
                AppError::BadRequest("reference audio_base64 is required".to_string())
            })?;
        let audio_bytes = decode_audio(audio_base64)?;
        references.push(InternalReference {
            voice_id: None,
            checksum: None,
            audio_bytes,
            content_type: reference
                .content_type
                .clone()
                .unwrap_or_else(|| "audio/wav".to_string()),
            text: reference.text.clone(),
        });
    }

    Ok(references)
}

fn normalized_payload(request: &AudioSpeechRequest) -> AppResult<Value> {
    let mut payload = serde_json::to_value(request).map_err(anyhow::Error::from)?;
    let object = payload
        .as_object_mut()
        .ok_or_else(|| AppError::BadRequest("request must be a JSON object".to_string()))?;
    object.remove("voice_id");
    object.remove("references");
    Ok(payload)
}

async fn non_streaming_inference(
    state: AppState,
    payload: Value,
    references: Vec<InternalReference>,
    api_kind: String,
) -> AppResult<Response> {
    let mut exclude = HashSet::new();

    loop {
        let (worker_id, mut rx) = dispatch_once(
            &state,
            payload.clone(),
            references.clone(),
            false,
            api_kind.clone(),
            &mut exclude,
        )
        .await?;

        let mut audio = BytesMut::new();
        let mut content_type = "application/octet-stream".to_string();
        let mut retry = false;

        while let Some(event) = rx.recv().await {
            match event {
                WorkerEvent::Chunk(chunk) => {
                    content_type = chunk.content_type;
                    audio.extend_from_slice(&chunk.bytes);
                }
                WorkerEvent::Done => {
                    return binary_response(StatusCode::OK, content_type, audio.freeze());
                }
                WorkerEvent::Error(error) => {
                    if should_retry(&state, &error, audio.is_empty()) {
                        exclude.insert(worker_id.clone());
                        retry = true;
                        break;
                    }
                    return Err(error_to_app_error(error));
                }
            }
        }

        if retry {
            continue;
        }

        if audio.is_empty() {
            exclude.insert(worker_id);
            continue;
        }

        return Err(AppError::Upstream(
            "worker disconnected before response completed".to_string(),
        ));
    }
}

async fn stream_inference(
    state: AppState,
    payload: Value,
    references: Vec<InternalReference>,
    api_kind: String,
) -> AppResult<Response> {
    let mut exclude = HashSet::new();

    loop {
        let (worker_id, mut rx) = dispatch_once(
            &state,
            payload.clone(),
            references.clone(),
            true,
            api_kind.clone(),
            &mut exclude,
        )
        .await?;

        match rx.recv().await {
            Some(WorkerEvent::Chunk(first_chunk)) => {
                let content_type = first_chunk.content_type;
                let first_bytes = first_chunk.bytes;
                let body_stream = stream! {
                    yield Ok::<Bytes, std::io::Error>(Bytes::from(first_bytes));
                    while let Some(event) = rx.recv().await {
                        match event {
                            WorkerEvent::Chunk(chunk) => yield Ok::<Bytes, std::io::Error>(Bytes::from(chunk.bytes)),
                            WorkerEvent::Done => break,
                            WorkerEvent::Error(error) => {
                                yield Err(std::io::Error::new(std::io::ErrorKind::Other, error.message));
                                break;
                            }
                        }
                    }
                };

                return Response::builder()
                    .status(StatusCode::OK)
                    .header(header::CONTENT_TYPE, content_type)
                    .body(Body::from_stream(body_stream))
                    .map_err(|error| AppError::Internal(anyhow::Error::from(error)));
            }
            Some(WorkerEvent::Done) => {
                return Response::builder()
                    .status(StatusCode::OK)
                    .header(header::CONTENT_TYPE, "application/octet-stream")
                    .body(Body::from(Bytes::new()))
                    .map_err(|error| AppError::Internal(anyhow::Error::from(error)));
            }
            Some(WorkerEvent::Error(error)) => {
                if should_retry(&state, &error, true) {
                    exclude.insert(worker_id);
                    continue;
                }
                return Err(error_to_app_error(error));
            }
            None => {
                exclude.insert(worker_id);
            }
        }
    }
}

async fn dispatch_once(
    state: &AppState,
    payload: Value,
    references: Vec<InternalReference>,
    stream: bool,
    api_kind: String,
    exclude: &mut HashSet<String>,
) -> AppResult<(String, mpsc::Receiver<WorkerEvent>)> {
    loop {
        let worker = state.select_worker(exclude).await?;
        let request_id = format!("req_{}", Uuid::new_v4().simple());
        let (tx, rx) = mpsc::channel(128);

        state.pending.insert(
            request_id.clone(),
            PendingRequest {
                worker_id: worker.worker_id.clone(),
                tx,
            },
        );
        worker.manager_inflight.fetch_add(1, Ordering::Relaxed);

        let message = WireMessage::InferenceRequest(InferenceRequest {
            request_id: request_id.clone(),
            api_kind: api_kind.clone(),
            payload: payload.clone(),
            references: references.clone(),
            stream,
            deadline_ms: None,
        });

        if worker.tx.send(message).await.is_err() {
            state.pending.remove(&request_id);
            worker.manager_inflight.fetch_sub(1, Ordering::Relaxed);
            exclude.insert(worker.worker_id);
            continue;
        }

        return Ok((worker.worker_id, rx));
    }
}

fn should_retry(state: &AppState, error: &InferenceError, no_bytes_sent: bool) -> bool {
    state.config.retry_on_worker_overload
        && no_bytes_sent
        && error.retryable
        && matches!(
            error.code.as_str(),
            "overloaded" | "worker_disconnected" | "sglang_unavailable"
        )
}

fn error_to_app_error(error: InferenceError) -> AppError {
    if error.code == "overloaded" {
        AppError::TooManyRequests(error.message)
    } else if error.code == "bad_request" {
        AppError::BadRequest(error.message)
    } else {
        AppError::Upstream(error.message)
    }
}

fn binary_response(status: StatusCode, content_type: String, bytes: Bytes) -> AppResult<Response> {
    Response::builder()
        .status(status)
        .header(header::CONTENT_TYPE, content_type)
        .body(Body::from(bytes))
        .map_err(|error| AppError::Internal(anyhow::Error::from(error)))
}

fn decode_audio(input: &str) -> AppResult<Vec<u8>> {
    let payload = input
        .split_once(',')
        .map(|(_, payload)| payload)
        .unwrap_or(input);

    STANDARD
        .decode(payload)
        .map_err(|_| AppError::BadRequest("reference audio is not valid base64".to_string()))
}
