use std::{collections::HashSet, sync::atomic::Ordering, time::Instant};

use async_stream::stream;
use axum::{
    body::Body,
    extract::{Multipart, Path, Query, State},
    http::{header, HeaderMap, HeaderValue, StatusCode},
    response::Response,
    routing::{delete, get, post},
    Json, Router,
};
use base64::{engine::general_purpose::STANDARD, Engine as _};
use bytes::{Bytes, BytesMut};
use chrono::Utc;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tokio::sync::mpsc;
use uuid::Uuid;

use crate::{
    auth::{require_admin_auth, require_openai_auth, require_openai_auth_for_priority, require_worker_auth},
    error::{AppError, AppResult},
    protocol::{InferenceError, InferenceRequest, InternalReference, Priority, RestartApiServer, RestartWorker, WireMessage},
    state::{AppState, PendingRequest, WorkerEvent},
    workers::worker_ws_handler,
};

const TARGET_WORKER_HEADER: &str = "x-fish-worker-id";
const TARGET_WORKER_HEADER_ALIAS: &str = "x-worker-id";
const PRIORITY_HEADER: &str = "x-fish-priority";

pub fn build_router(state: AppState) -> Router {
    Router::new()
        .route("/health", get(health))
        .route("/v1/workers", get(list_workers))
        .route("/v1/audio/speech", post(audio_speech))
        .route("/v1/tts", post(fish_tts))
        .route("/v1/references/add", post(add_reference))
        .route("/v1/references/list", get(list_references))
        .route(
            "/v1/references/delete",
            delete(delete_reference).post(delete_reference),
        )
        .route("/v1/references/update", post(update_reference))
        .route(
            "/internal/voices/:voice_id/audio",
            get(get_internal_voice_audio),
        )
        .route("/internal/workers/ws", get(worker_ws_handler))
        .route("/internal/admin/workers", get(admin_list_workers))
        .route("/internal/admin/metrics", get(admin_get_metrics))
        .route(
            "/internal/admin/workers/:worker_id/restart_api",
            post(admin_restart_api),
        )
        .route(
            "/internal/admin/workers/:worker_id/restart_worker",
            post(admin_restart_worker),
        )
        .with_state(state)
}

async fn health() -> Json<Value> {
    Json(json!({ "status": "ok" }))
}

async fn list_workers(State(state): State<AppState>, headers: HeaderMap) -> AppResult<Json<Value>> {
    require_openai_auth(&headers, &state.config)?;

    let now = Utc::now();
    let mut workers = Vec::new();
    let mut available_workers = 0_u32;
    let mut stale_workers = 0_u32;
    for entry in state.workers.iter() {
        let handle = entry.value().clone();
        let mut status = handle.status.read().await.clone();
        status.manager_inflight = handle.manager_inflight.load(Ordering::Relaxed);
        status.heartbeat_age_ms = (now - status.last_heartbeat_at).num_milliseconds().max(0);
        let heartbeat_age_seconds = status.heartbeat_age_ms / 1000;
        let stale = heartbeat_age_seconds > state.config.worker_heartbeat_stale_after_seconds;
        if stale {
            stale_workers += 1;
        } else if status.ready && status.sglang_healthy {
            available_workers += 1;
        }
        workers.push(status);
    }
    if available_workers == 0 {
        tracing::warn!(
            total_workers = workers.len(),
            stale_workers,
            heartbeat_stale_after_seconds = state.config.worker_heartbeat_stale_after_seconds,
            "workers API returned no available workers"
        );
    } else {
        tracing::debug!(
            total_workers = workers.len(),
            available_workers,
            stale_workers,
            "workers API listed workers"
        );
    }

    Ok(Json(json!({ "data": workers })))
}

async fn admin_list_workers(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> AppResult<Json<Value>> {
    require_admin_auth(&headers, &state.config)?;

    let now = Utc::now();
    let mut workers = Vec::new();
    for entry in state.workers.iter() {
        let handle = entry.value().clone();
        let mut status = handle.status.read().await.clone();
        status.manager_inflight = handle.manager_inflight.load(Ordering::Relaxed);
        status.heartbeat_age_ms = (now - status.last_heartbeat_at).num_milliseconds().max(0);
        workers.push(status);
    }

    Ok(Json(json!({ "data": workers })))
}

async fn admin_restart_api(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(worker_id): Path<String>,
) -> AppResult<Json<Value>> {
    require_admin_auth(&headers, &state.config)?;

    let worker = state.select_worker_by_id(&worker_id).await?;
    let msg = WireMessage::RestartApiServer(RestartApiServer {
        reason: Some("admin requested restart".to_string()),
    });

    if worker.tx.send(msg).await.is_err() {
        return Err(AppError::Internal(anyhow::anyhow!("failed to send restart command to worker")));
    }

    Ok(Json(json!({ "success": true, "message": "Restart API server command sent" })))
}

async fn admin_restart_worker(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(worker_id): Path<String>,
) -> AppResult<Json<Value>> {
    require_admin_auth(&headers, &state.config)?;

    let worker = state.select_worker_by_id(&worker_id).await?;
    let msg = WireMessage::RestartWorker(RestartWorker {
        reason: Some("admin requested restart".to_string()),
    });

    if worker.tx.send(msg).await.is_err() {
        return Err(AppError::Internal(anyhow::anyhow!("failed to send restart command to worker")));
    }

    Ok(Json(json!({ "success": true, "message": "Restart worker command sent" })))
}

async fn add_reference(
    State(state): State<AppState>,
    headers: HeaderMap,
    mut multipart: Multipart,
) -> AppResult<Json<Value>> {
    require_openai_auth(&headers, &state.config)?;

    let mut id = None;
    let mut text = None;
    let mut audio = None;
    let mut content_type = None;

    while let Some(field) = multipart
        .next_field()
        .await
        .map_err(|error| AppError::BadRequest(format!("invalid multipart form: {error}")))?
    {
        let name = field.name().unwrap_or_default().to_string();
        match name.as_str() {
            "id" => {
                id = Some(field.text().await.map_err(|error| {
                    AppError::BadRequest(format!("invalid reference id field: {error}"))
                })?);
            }
            "text" => {
                text = Some(field.text().await.map_err(|error| {
                    AppError::BadRequest(format!("invalid reference text field: {error}"))
                })?);
            }
            "audio" => {
                content_type = field.content_type().map(ToOwned::to_owned);
                audio = Some(field.bytes().await.map_err(|error| {
                    AppError::BadRequest(format!("invalid reference audio field: {error}"))
                })?);
            }
            _ => {}
        }
    }

    let id = required_reference_field(id, "id")?;
    validate_reference_id(&id)?;
    let text = required_reference_field(text, "text")?;
    if text.trim().is_empty() {
        return Err(AppError::BadRequest(
            "Reference text cannot be empty".to_string(),
        ));
    }
    let audio = audio.ok_or_else(|| AppError::BadRequest("audio is required".to_string()))?;

    state
        .voice_store
        .create_voice_from_bytes(Some(id.clone()), text, audio.to_vec(), content_type)
        .await?;

    Ok(Json(json!({
        "success": true,
        "message": format!("Reference voice '{id}' added successfully"),
        "reference_id": id,
    })))
}

async fn list_references(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> AppResult<Json<Value>> {
    require_openai_auth(&headers, &state.config)?;
    let reference_ids = state.voice_store.list_voice_ids().await?;
    Ok(Json(json!({
        "success": true,
        "reference_ids": reference_ids,
        "message": format!("Found {} reference voices", reference_ids.len()),
    })))
}

#[derive(Debug, Deserialize)]
struct DeleteReferenceRequest {
    reference_id: String,
}

async fn delete_reference(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(request): Json<DeleteReferenceRequest>,
) -> AppResult<Json<Value>> {
    require_openai_auth(&headers, &state.config)?;
    validate_reference_id(&request.reference_id)?;

    if state
        .voice_store
        .get_voice(&request.reference_id)
        .await?
        .is_none()
    {
        return Err(AppError::NotFound(format!(
            "Reference ID '{}' not found",
            request.reference_id
        )));
    }

    state
        .voice_store
        .delete_voice(&request.reference_id)
        .await?;
    Ok(Json(json!({
        "success": true,
        "message": format!("Reference voice '{}' deleted successfully", request.reference_id),
        "reference_id": request.reference_id,
    })))
}

#[derive(Debug, Deserialize)]
struct UpdateReferenceRequest {
    old_reference_id: String,
    new_reference_id: String,
}

async fn update_reference(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(request): Json<UpdateReferenceRequest>,
) -> AppResult<Json<Value>> {
    require_openai_auth(&headers, &state.config)?;
    validate_reference_id(&request.old_reference_id)?;
    validate_reference_id(&request.new_reference_id)?;

    state
        .voice_store
        .rename_voice(&request.old_reference_id, &request.new_reference_id)
        .await?;

    Ok(Json(json!({
        "success": true,
        "message": format!(
            "Reference voice renamed from '{}' to '{}' successfully",
            request.old_reference_id, request.new_reference_id
        ),
        "old_reference_id": request.old_reference_id,
        "new_reference_id": request.new_reference_id,
    })))
}

fn required_reference_field(value: Option<String>, name: &str) -> AppResult<String> {
    value
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
        .ok_or_else(|| AppError::BadRequest(format!("{name} is required")))
}

fn validate_reference_id(reference_id: &str) -> AppResult<()> {
    if reference_id.trim().is_empty() {
        return Err(AppError::BadRequest(
            "Reference ID cannot be empty".to_string(),
        ));
    }
    if reference_id.len() > 255
        || !reference_id
            .chars()
            .all(|ch| ch.is_ascii_alphanumeric() || matches!(ch, '-' | '_' | ' '))
    {
        return Err(AppError::BadRequest(
            "Reference ID contains invalid characters or is too long".to_string(),
        ));
    }
    Ok(())
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
    let started = Instant::now();
    require_worker_auth(&headers, &state.config)?;
    tracing::info!(voice_id = %voice_id, checksum = ?query.checksum, "worker requested voice audio");
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
        tracing::warn!(
            voice_id = %voice_id,
            requested_checksum = ?query.checksum,
            current_checksum = %voice.checksum,
            "worker requested stale or unknown voice checksum"
        );
        return Err(AppError::NotFound("voice checksum not found".to_string()));
    }

    let audio = state.voice_store.get_voice_audio(&voice).await?;
    let size_bytes = audio.len();
    let mut response = binary_response(StatusCode::OK, voice.content_type, audio)?;
    response.headers_mut().insert(
        "x-voice-checksum",
        HeaderValue::from_str(&voice.checksum)
            .map_err(|error| AppError::Internal(anyhow::Error::from(error)))?,
    );
    tracing::info!(
        voice_id = %voice_id,
        checksum = %voice.checksum,
        size_bytes,
        elapsed_ms = elapsed_ms(started),
        "served voice audio to worker"
    );
    Ok(response)
}

#[derive(Debug, Clone, Default, Deserialize, Serialize, PartialEq)]
struct IndexExtraParams {
    #[serde(default)]
    #[serde(skip_serializing_if = "Option::is_none")]
    lang: Option<String>,
    #[serde(default)]
    #[serde(skip_serializing_if = "Option::is_none")]
    text_normalization: Option<bool>,
    #[serde(default)]
    #[serde(skip_serializing_if = "Option::is_none")]
    emo_vector: Option<Vec<f64>>,
    #[serde(default)]
    #[serde(alias = "emo_weight")]
    #[serde(skip_serializing_if = "Option::is_none")]
    emo_alpha: Option<f64>,
    #[serde(default)]
    #[serde(skip_serializing_if = "Option::is_none")]
    emo_text: Option<String>,
    #[serde(default)]
    #[serde(alias = "auto_emotion")]
    #[serde(skip_serializing_if = "Option::is_none")]
    use_emo_text: Option<bool>,
    #[serde(default)]
    #[serde(skip_serializing_if = "Option::is_none")]
    emo_audio: Option<String>,
    #[serde(default)]
    #[serde(skip_serializing_if = "Option::is_none")]
    emo_audio_base64: Option<String>,
    #[serde(flatten)]
    extra: serde_json::Map<String, Value>,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
struct AudioSpeechRequest {
    input: String,
    #[serde(default)]
    #[serde(skip_serializing_if = "Option::is_none")]
    model: Option<String>,
    #[serde(default)]
    #[serde(alias = "voice_id")]
    #[serde(skip_serializing_if = "Option::is_none")]
    voice: Option<String>,
    #[serde(default)]
    #[serde(alias = "reference_audio")]
    #[serde(skip_serializing_if = "Option::is_none")]
    ref_audio: Option<String>,
    #[serde(default)]
    references: Vec<ClientReference>,
    #[serde(default)]
    #[serde(alias = "format")]
    #[serde(skip_serializing_if = "Option::is_none")]
    response_format: Option<String>,
    #[serde(default)]
    #[serde(alias = "streaming")]
    #[serde(skip_serializing_if = "Option::is_none")]
    stream: Option<bool>,
    #[serde(default)]
    #[serde(skip_serializing_if = "Option::is_none")]
    speed: Option<f64>,
    #[serde(default)]
    #[serde(skip_serializing_if = "Option::is_none")]
    duration_factor: Option<f64>,

    #[serde(default)]
    #[serde(skip_serializing_if = "Option::is_none")]
    extra_params: Option<IndexExtraParams>,

    #[serde(default)]
    #[serde(skip_serializing_if = "Option::is_none")]
    lang: Option<String>,
    #[serde(default)]
    #[serde(skip_serializing_if = "Option::is_none")]
    text_normalization: Option<bool>,
    #[serde(default)]
    #[serde(skip_serializing_if = "Option::is_none")]
    emo_audio: Option<String>,
    #[serde(default)]
    #[serde(skip_serializing_if = "Option::is_none")]
    emo_audio_base64: Option<String>,
    #[serde(default)]
    #[serde(alias = "emo_weight")]
    #[serde(skip_serializing_if = "Option::is_none")]
    emo_alpha: Option<f64>,
    #[serde(default)]
    #[serde(skip_serializing_if = "Option::is_none")]
    emo_vector: Option<Vec<f64>>,
    #[serde(default)]
    #[serde(skip_serializing_if = "Option::is_none")]
    emo_text: Option<String>,
    #[serde(default)]
    #[serde(alias = "auto_emotion")]
    #[serde(skip_serializing_if = "Option::is_none")]
    use_emo_text: Option<bool>,
    #[serde(flatten)]
    extra: serde_json::Map<String, Value>,
}

impl AudioSpeechRequest {
    fn resolved_lang(&self) -> Option<&str> {
        self.extra_params
            .as_ref()
            .and_then(|p| p.lang.as_deref())
            .or(self.lang.as_deref())
    }

    fn resolved_text_normalization(&self) -> bool {
        self.extra_params
            .as_ref()
            .and_then(|p| p.text_normalization)
            .or(self.text_normalization)
            .unwrap_or(true)
    }

    fn resolved_emo_vector(&self) -> Option<&[f64]> {
        self.extra_params
            .as_ref()
            .and_then(|p| p.emo_vector.as_deref())
            .or(self.emo_vector.as_deref())
    }

    fn resolved_emo_alpha(&self) -> Option<f64> {
        self.extra_params
            .as_ref()
            .and_then(|p| p.emo_alpha)
            .or(self.emo_alpha)
    }

    fn resolved_emo_text(&self) -> Option<&str> {
        self.extra_params
            .as_ref()
            .and_then(|p| p.emo_text.as_deref())
            .or(self.emo_text.as_deref())
    }

    fn resolved_use_emo_text(&self) -> Option<bool> {
        self.extra_params
            .as_ref()
            .and_then(|p| p.use_emo_text)
            .or(self.use_emo_text)
            .or_else(|| {
                if self.resolved_emo_text().is_some() {
                    Some(true)
                } else {
                    None
                }
            })
    }

    fn resolved_emo_audio(&self) -> Option<&str> {
        self.extra_params
            .as_ref()
            .and_then(|p| p.emo_audio.as_deref().or(p.emo_audio_base64.as_deref()))
            .or(self.emo_audio.as_deref())
            .or(self.emo_audio_base64.as_deref())
    }
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
struct Prosody {
    #[serde(default)]
    speed: Option<f64>,
}

#[derive(Debug, Deserialize)]
struct FishTtsRequest {
    text: String,
    #[serde(default)]
    model: Option<String>,
    #[serde(default)]
    reference_id: Option<String>,
    #[serde(default)]
    #[serde(alias = "reference_audio")]
    ref_audio: Option<String>,
    #[serde(default)]
    references: Vec<ClientReference>,
    #[serde(default)]
    format: Option<String>,
    #[serde(default)]
    streaming: Option<bool>,
    #[serde(default)]
    speed: Option<f64>,
    #[serde(default)]
    duration_factor: Option<f64>,
    #[serde(default)]
    extra_params: Option<IndexExtraParams>,
    #[serde(default)]
    emo_audio: Option<String>,
    #[serde(default)]
    emo_audio_base64: Option<String>,
    #[serde(default)]
    #[serde(alias = "emo_weight")]
    emo_alpha: Option<f64>,
    #[serde(default)]
    emo_vector: Option<Vec<f64>>,
    #[serde(default)]
    emo_text: Option<String>,
    #[serde(default)]
    #[serde(alias = "auto_emotion")]
    use_emo_text: Option<bool>,
    #[serde(default)]
    lang: Option<String>,
    #[serde(default)]
    text_normalization: Option<bool>,
    #[serde(default)]
    prosody: Option<Prosody>,
    #[serde(flatten)]
    extra: serde_json::Map<String, Value>,
}

async fn fish_tts(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(request): Json<FishTtsRequest>,
) -> AppResult<Response> {
    let priority = priority_from_headers(&headers)?;
    require_openai_auth_for_priority(&headers, &state.config, priority)?;
    let target_worker_id = target_worker_id_from_headers(&headers)?;

    let speed = request.speed.or_else(|| request.prosody.as_ref().and_then(|p| p.speed));

    let mapped = AudioSpeechRequest {
        input: request.text,
        model: request.model,
        voice: request.reference_id,
        ref_audio: request.ref_audio,
        references: request.references,
        response_format: request.format,
        stream: request.streaming,
        speed,
        duration_factor: request.duration_factor,
        extra_params: request.extra_params,
        emo_audio: request.emo_audio,
        emo_audio_base64: request.emo_audio_base64,
        emo_alpha: request.emo_alpha,
        emo_vector: request.emo_vector,
        emo_text: request.emo_text,
        use_emo_text: request.use_emo_text,
        lang: request.lang,
        text_normalization: request.text_normalization,
        extra: request.extra,
    };

    handle_speech(state, mapped, "fish_tts", target_worker_id, priority).await
}

async fn audio_speech(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(request): Json<AudioSpeechRequest>,
) -> AppResult<Response> {
    let priority = priority_from_headers(&headers)?;
    require_openai_auth_for_priority(&headers, &state.config, priority)?;
    let target_worker_id = target_worker_id_from_headers(&headers)?;
    handle_speech(state, request, "audio_speech", target_worker_id, priority).await
}

async fn handle_speech(
    state: AppState,
    request: AudioSpeechRequest,
    api_kind: &str,
    target_worker_id: Option<String>,
    priority: Priority,
) -> AppResult<Response> {
    let started = Instant::now();
    if request.input.trim().is_empty() {
        return Err(AppError::BadRequest("input must not be empty".to_string()));
    }
    validate_speech_parameters(&request)?;

    let target_model = state.resolve_target_model(request.model.as_deref()).await;
    let stream_response = request.stream.unwrap_or(false);
    let input_chars = request.input.chars().count();
    let reference_started = Instant::now();
    let references = match resolve_references(&state, &request).await {
        Ok(references) => references,
        Err(error) => {
            tracing::warn!(
                api_kind,
                target_model = %target_model,
                stream = stream_response,
                input_chars,
                total_ms = elapsed_ms(started),
                manager_reference_ms = elapsed_ms(reference_started),
                "speech request failed while resolving references"
            );
            return Err(error);
        }
    };
    let manager_reference_ms = elapsed_ms(reference_started);
    let reference_summary = reference_summary(&references);
    tracing::info!(
        api_kind,
        target_model = %target_model,
        stream = stream_response,
        input_chars,
        references = reference_summary.total,
        metadata_references = reference_summary.metadata,
        inline_references = reference_summary.inline,
        inline_audio_bytes = reference_summary.inline_audio_bytes,
        target_worker_id = ?target_worker_id,
        priority = %priority.as_str(),
        manager_reference_ms,
        "accepted speech request"
    );
    let payload = normalized_payload(&request, &target_model)?;
    let trace = SpeechTrace {
        started,
        api_kind: api_kind.to_string(),
        target_model,
        input_chars,
        stream: stream_response,
        manager_reference_ms,
        reference_summary,
        target_worker_id,
        priority,
    };

    if stream_response {
        stream_inference(state, payload, references, trace).await
    } else {
        non_streaming_inference(state, payload, references, trace).await
    }
}

async fn resolve_references(
    state: &AppState,
    request: &AudioSpeechRequest,
) -> AppResult<Vec<InternalReference>> {
    let mut references = Vec::new();

    if let Some(voice_id) = &request.voice {
        let voice = state
            .voice_store
            .get_voice(voice_id)
            .await?
            .ok_or_else(|| AppError::NotFound("voice not found".to_string()))?;
        tracing::info!(
            voice_id = %voice.voice_id,
            checksum = %voice.checksum,
            content_type = %voice.content_type,
            size_bytes = voice.size_bytes,
            "resolved stored voice reference"
        );
        references.push(InternalReference {
            voice_id: Some(voice.voice_id),
            checksum: Some(voice.checksum),
            audio_bytes: Vec::new(),
            content_type: voice.content_type,
            text: voice.text,
        });
    }

    if let Some(ref_audio) = &request.ref_audio {
        let trimmed = ref_audio.trim();
        if !trimmed.is_empty() {
            if trimmed.starts_with("data:")
                || (!trimmed.starts_with("http://")
                    && !trimmed.starts_with("https://")
                    && !trimmed.starts_with("file://")
                    && !trimmed.starts_with('/')
                    && !trimmed.contains('\n'))
            {
                if let Ok(audio_bytes) = decode_audio(trimmed) {
                    tracing::info!(
                        size_bytes = audio_bytes.len(),
                        "resolved ref_audio base64 / data URL reference"
                    );
                    references.push(InternalReference {
                        voice_id: None,
                        checksum: None,
                        audio_bytes,
                        content_type: "audio/wav".to_string(),
                        text: "".to_string(),
                    });
                }
            }
        }
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
        tracing::info!(
            content_type = %reference.content_type.as_deref().unwrap_or("audio/wav"),
            size_bytes = audio_bytes.len(),
            "resolved inline audio reference"
        );
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

fn target_worker_id_from_headers(headers: &HeaderMap) -> AppResult<Option<String>> {
    let Some(value) = headers
        .get(TARGET_WORKER_HEADER)
        .or_else(|| headers.get(TARGET_WORKER_HEADER_ALIAS))
    else {
        return Ok(None);
    };

    let worker_id = value
        .to_str()
        .map_err(|_| AppError::BadRequest("target worker header must be valid UTF-8".to_string()))?
        .trim()
        .to_string();
    if worker_id.is_empty() {
        return Err(AppError::BadRequest(
            "target worker header must not be empty".to_string(),
        ));
    }

    Ok(Some(worker_id))
}

fn priority_from_headers(headers: &HeaderMap) -> AppResult<Priority> {
    let Some(value) = headers.get(PRIORITY_HEADER) else {
        return Ok(Priority::default());
    };

    let value = value
        .to_str()
        .map_err(|_| AppError::BadRequest("priority header must be valid UTF-8".to_string()))?;
    Priority::parse(value).ok_or_else(|| {
        AppError::BadRequest("priority header must be one of v1, v2, v3, v4".to_string())
    })
}

fn validate_speech_parameters(request: &AudioSpeechRequest) -> AppResult<()> {
    if let Some(speed) = request.speed {
        if !(0.5..=2.0).contains(&speed) {
            return Err(AppError::BadRequest(
                "speed must be between 0.5 and 2.0".to_string(),
            ));
        }
    }
    if let Some(df) = request.duration_factor {
        if !(0.5..=2.0).contains(&df) {
            return Err(AppError::BadRequest(
                "duration_factor must be between 0.5 and 2.0".to_string(),
            ));
        }
    }
    if let Some(alpha) = request.resolved_emo_alpha() {
        if !(0.0..=1.0).contains(&alpha) {
            return Err(AppError::BadRequest(
                "emo_alpha must be between 0.0 and 1.0".to_string(),
            ));
        }
    }
    if let Some(vec) = request.resolved_emo_vector() {
        if vec.len() != 8 {
            return Err(AppError::BadRequest(
                "emo_vector must contain exactly 8 elements".to_string(),
            ));
        }
    }
    if let Some(lang) = request.resolved_lang() {
        let upper = lang.trim().to_ascii_uppercase();
        if !["ZH", "EN", "JA", "ES", "AR", "ZHEN"].contains(&upper.as_str()) {
            return Err(AppError::BadRequest(
                format!("lang '{}' is not supported (allowed: zh, en, ja, es, ar, zhen)", lang),
            ));
        }
    }
    Ok(())
}

fn is_fish_model(model: &str) -> bool {
    let lower = model.to_ascii_lowercase();
    lower.contains("fish") || lower.contains("s2-pro")
}

fn normalized_payload(request: &AudioSpeechRequest, target_model: &str) -> AppResult<Value> {
    let mut payload = serde_json::to_value(request).map_err(anyhow::Error::from)?;
    let object = payload
        .as_object_mut()
        .ok_or_else(|| AppError::BadRequest("request must be a JSON object".to_string()))?;
    object.remove("voice");
    object.remove("voice_id");
    object.remove("references");
    if is_fish_model(target_model) {
        object.remove("ref_audio");
        object.remove("reference_audio");
        object.remove("extra_params");
        object.remove("emo_audio");
        object.remove("emo_audio_base64");
        object.remove("emo_alpha");
        object.remove("emo_weight");
        object.remove("emo_vector");
        object.remove("emo_text");
        object.remove("use_emo_text");
        object.remove("auto_emotion");
        object.remove("duration_factor");
        object.remove("lang");
        object.remove("text_normalization");
    } else {
        let mut extra_map = match object.get("extra_params").and_then(|v| v.as_object()).cloned() {
            Some(m) => m,
            None => serde_json::Map::new(),
        };

        if let Some(lang) = request.resolved_lang() {
            extra_map.insert("lang".to_string(), Value::String(lang.to_string()));
            object.insert("lang".to_string(), Value::String(lang.to_string()));
        }
        let norm = request.resolved_text_normalization();
        extra_map.insert("text_normalization".to_string(), Value::Bool(norm));
        object.insert("text_normalization".to_string(), Value::Bool(norm));

        if let Some(vec) = request.resolved_emo_vector() {
            let v = serde_json::to_value(vec).unwrap_or(Value::Null);
            extra_map.insert("emo_vector".to_string(), v.clone());
            object.insert("emo_vector".to_string(), v);
        }
        if let Some(alpha) = request.resolved_emo_alpha() {
            let v = Value::from(alpha);
            extra_map.insert("emo_alpha".to_string(), v.clone());
            object.insert("emo_alpha".to_string(), v);
        }
        if let Some(txt) = request.resolved_emo_text() {
            let v = Value::String(txt.to_string());
            extra_map.insert("emo_text".to_string(), v.clone());
            object.insert("emo_text".to_string(), v);
        }
        if let Some(use_txt) = request.resolved_use_emo_text() {
            let v = Value::Bool(use_txt);
            extra_map.insert("use_emo_text".to_string(), v.clone());
            object.insert("use_emo_text".to_string(), v);
        }
        if let Some(aud) = request.resolved_emo_audio() {
            let v = Value::String(aud.to_string());
            extra_map.insert("emo_audio".to_string(), v.clone());
            object.insert("emo_audio".to_string(), v);
        }

        object.insert("extra_params".to_string(), Value::Object(extra_map));
    }
    Ok(payload)
}

async fn non_streaming_inference(
    state: AppState,
    payload: Value,
    references: Vec<InternalReference>,
    trace: SpeechTrace,
) -> AppResult<Response> {
    let mut exclude = HashSet::new();

    loop {
        let dispatch = dispatch_once(
            &state,
            payload.clone(),
            references.clone(),
            false,
            trace.api_kind.clone(),
            &trace,
            &mut exclude,
        )
        .await?;
        let worker_id = dispatch.worker_id.clone();
        let request_id = dispatch.request_id.clone();
        let dispatch_timing = DispatchTiming {
            worker_id: dispatch.worker_id.clone(),
            request_id: dispatch.request_id.clone(),
            dispatch_ms: dispatch.dispatch_ms,
            dispatched_at: dispatch.dispatched_at,
        };
        let mut rx = dispatch.rx;

        let mut audio = BytesMut::new();
        let mut content_type = "application/octet-stream".to_string();
        let mut retry = false;

        while let Some(event) = rx.recv().await {
            match event {
                WorkerEvent::Chunk(chunk) => {
                    content_type = chunk.content_type;
                    audio.extend_from_slice(&chunk.bytes);
                }
                WorkerEvent::Done(done) => {
                    log_speech_completed(
                        &trace,
                        &dispatch_timing,
                        &content_type,
                        audio.len() as u64,
                        &done,
                    );
                    let status = if done.finish_reason.as_deref() == Some("length")
                        || done.http_status == Some(StatusCode::PARTIAL_CONTENT.as_u16())
                    {
                        StatusCode::PARTIAL_CONTENT
                    } else {
                        StatusCode::OK
                    };
                    let mut response = binary_response(status, content_type, audio.freeze())?;
                    add_tts_completion_headers(response.headers_mut(), &done)?;
                    return Ok(response);
                }
                WorkerEvent::Error(error) => {
                    tracing::warn!(
                            worker_id = %worker_id,
                            request_id = %error.request_id,
                            api_kind = %trace.api_kind,
                            code = %error.code,
                            retryable = error.retryable,
                            message = %error.message,
                            total_ms = elapsed_ms(trace.started),
                            manager_reference_ms = trace.manager_reference_ms,
                            manager_dispatch_ms = dispatch_timing.dispatch_ms,
                            worker_roundtrip_ms = elapsed_ms(dispatch_timing.dispatched_at),
                            "worker returned non-streaming inference error"
                    );
                    if should_retry(&state, &trace, &error, audio.is_empty()) {
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
            tracing::warn!(
                worker_id = %worker_id,
                request_id = %request_id,
                api_kind = %trace.api_kind,
                total_ms = elapsed_ms(trace.started),
                manager_reference_ms = trace.manager_reference_ms,
                manager_dispatch_ms = dispatch_timing.dispatch_ms,
                worker_roundtrip_ms = elapsed_ms(dispatch_timing.dispatched_at),
                "worker disconnected before producing audio"
            );
            if trace.target_worker_id.is_some() {
                return Err(AppError::Upstream(
                    "selected worker disconnected before producing audio".to_string(),
                ));
            }
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
    trace: SpeechTrace,
) -> AppResult<Response> {
    let mut exclude = HashSet::new();

    loop {
        let dispatch = dispatch_once(
            &state,
            payload.clone(),
            references.clone(),
            true,
            trace.api_kind.clone(),
            &trace,
            &mut exclude,
        )
        .await?;
        let worker_id = dispatch.worker_id.clone();
        let request_id = dispatch.request_id.clone();
        let dispatch_timing = DispatchTiming {
            worker_id: dispatch.worker_id.clone(),
            request_id: dispatch.request_id.clone(),
            dispatch_ms: dispatch.dispatch_ms,
            dispatched_at: dispatch.dispatched_at,
        };
        let mut rx = dispatch.rx;

        match rx.recv().await {
            Some(WorkerEvent::Chunk(first_chunk)) => {
                let content_type = first_chunk.content_type;
                let first_bytes = first_chunk.bytes;
                let body_worker_id = worker_id.clone();
                let body_request_id = request_id.clone();
                let body_content_type = content_type.clone();
                let body_dispatch = dispatch_timing.clone();
                let body_trace = trace.clone();
                tracing::info!(
                    worker_id = %worker_id,
                    request_id = %request_id,
                    api_kind = %trace.api_kind,
                    first_chunk_bytes = first_bytes.len(),
                    first_byte_ms = elapsed_ms(dispatch_timing.dispatched_at),
                    total_ms = elapsed_ms(trace.started),
                    manager_reference_ms = trace.manager_reference_ms,
                    manager_dispatch_ms = dispatch_timing.dispatch_ms,
                    "speech stream response started"
                );
                let body_stream = stream! {
                    let mut streamed_bytes = first_bytes.len() as u64;
                    let mut streamed_chunks = 1_u64;
                    yield Ok::<Bytes, std::io::Error>(Bytes::from(first_bytes));
                    while let Some(event) = rx.recv().await {
                        match event {
                            WorkerEvent::Chunk(chunk) => {
                                streamed_bytes += chunk.bytes.len() as u64;
                                streamed_chunks += 1;
                                yield Ok::<Bytes, std::io::Error>(Bytes::from(chunk.bytes));
                            }
                            WorkerEvent::Done(done) => {
                                log_speech_completed(
                                    &body_trace,
                                    &body_dispatch,
                                    &body_content_type,
                                    streamed_bytes,
                                    &done,
                                );
                                if done.chunks.is_some_and(|chunks| chunks != streamed_chunks) {
                                    tracing::warn!(
                                        worker_id = %body_worker_id,
                                        request_id = %body_request_id,
                                        streamed_chunks,
                                        worker_chunks = ?done.chunks,
                                        "streamed chunk count differed from worker report"
                                    );
                                }
                                break;
                            }
                            WorkerEvent::Error(error) => {
                                tracing::warn!(
                                    worker_id = %body_worker_id,
                                    request_id = %error.request_id,
                                    api_kind = %body_trace.api_kind,
                                    code = %error.code,
                                    retryable = error.retryable,
                                    message = %error.message,
                                    total_ms = elapsed_ms(body_trace.started),
                                    manager_reference_ms = body_trace.manager_reference_ms,
                                    manager_dispatch_ms = body_dispatch.dispatch_ms,
                                    worker_roundtrip_ms = elapsed_ms(body_dispatch.dispatched_at),
                                    "worker returned streaming inference error after first chunk"
                                );
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
            Some(WorkerEvent::Done(done)) => {
                log_speech_completed(
                    &trace,
                    &dispatch_timing,
                    "application/octet-stream",
                    0,
                    &done,
                );
                return Response::builder()
                    .status(StatusCode::OK)
                    .header(header::CONTENT_TYPE, "application/octet-stream")
                    .body(Body::from(Bytes::new()))
                    .map_err(|error| AppError::Internal(anyhow::Error::from(error)));
            }
            Some(WorkerEvent::Error(error)) => {
                tracing::warn!(
                    worker_id = %worker_id,
                    request_id = %error.request_id,
                    api_kind = %trace.api_kind,
                    code = %error.code,
                    retryable = error.retryable,
                    message = %error.message,
                    total_ms = elapsed_ms(trace.started),
                    manager_reference_ms = trace.manager_reference_ms,
                    manager_dispatch_ms = dispatch_timing.dispatch_ms,
                    worker_roundtrip_ms = elapsed_ms(dispatch_timing.dispatched_at),
                    "worker returned streaming inference error"
                );
                if should_retry(&state, &trace, &error, true) {
                    exclude.insert(worker_id);
                    continue;
                }
                return Err(error_to_app_error(error));
            }
            None => {
                if trace.target_worker_id.is_some() {
                    return Err(AppError::Upstream(
                        "selected worker disconnected before response completed".to_string(),
                    ));
                }
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
    trace: &SpeechTrace,
    exclude: &mut HashSet<String>,
) -> AppResult<DispatchResult> {
    loop {
        let started = Instant::now();
        let worker = if let Some(worker_id) = &trace.target_worker_id {
            state.select_worker_by_id(worker_id).await?
        } else {
            state.select_worker(&trace.target_model, exclude, trace.priority).await?
        };
        let request_id = format!("req_{}", Uuid::new_v4().simple());
        let (tx, rx) = mpsc::channel(128);

        state.pending.insert(
            request_id.clone(),
            PendingRequest {
                worker_id: worker.worker_id.clone(),
                connection_id: worker.connection_id.clone(),
                tx,
            },
        );
        worker.manager_inflight.fetch_add(1, Ordering::Relaxed);

        let message = WireMessage::InferenceRequest(InferenceRequest {
            request_id: request_id.clone(),
            api_kind: api_kind.clone(),
            priority: trace.priority,
            payload: payload.clone(),
            references: references.clone(),
            stream,
            deadline_ms: None,
        });
        let reference_summary = reference_summary(&references);
        tracing::info!(
            worker_id = %worker.worker_id,
            connection_id = %worker.connection_id,
            request_id = %request_id,
            target_model = %trace.target_model,
            api_kind = %api_kind,
            priority = %trace.priority.as_str(),
            stream,
            references = reference_summary.total,
            metadata_references = reference_summary.metadata,
            inline_references = reference_summary.inline,
            inline_audio_bytes = reference_summary.inline_audio_bytes,
            excluded_workers = exclude.len(),
            "dispatching inference request to worker"
        );

        if worker.tx.send(message).await.is_err() {
            tracing::warn!(
                worker_id = %worker.worker_id,
                connection_id = %worker.connection_id,
                request_id = %request_id,
                "failed to send inference request to worker"
            );
            state.pending.remove(&request_id);
            worker.manager_inflight.fetch_sub(1, Ordering::Relaxed);
            if trace.target_worker_id.is_some() {
                return Err(AppError::Upstream(format!(
                    "selected worker '{}' is unavailable",
                    worker.worker_id
                )));
            }
            exclude.insert(worker.worker_id);
            continue;
        }

        return Ok(DispatchResult {
            worker_id: worker.worker_id,
            request_id,
            rx,
            dispatch_ms: elapsed_ms(started),
            dispatched_at: Instant::now(),
        });
    }
}

struct DispatchResult {
    worker_id: String,
    request_id: String,
    rx: mpsc::Receiver<WorkerEvent>,
    dispatch_ms: f64,
    dispatched_at: Instant,
}

#[derive(Clone)]
struct DispatchTiming {
    worker_id: String,
    request_id: String,
    dispatch_ms: f64,
    dispatched_at: Instant,
}

trait DispatchMetrics {
    fn worker_id(&self) -> &str;
    fn request_id(&self) -> &str;
    fn dispatch_ms(&self) -> f64;
    fn dispatched_at(&self) -> Instant;
}

impl DispatchMetrics for DispatchResult {
    fn worker_id(&self) -> &str {
        &self.worker_id
    }

    fn request_id(&self) -> &str {
        &self.request_id
    }

    fn dispatch_ms(&self) -> f64 {
        self.dispatch_ms
    }

    fn dispatched_at(&self) -> Instant {
        self.dispatched_at
    }
}

impl DispatchMetrics for DispatchTiming {
    fn worker_id(&self) -> &str {
        &self.worker_id
    }

    fn request_id(&self) -> &str {
        &self.request_id
    }

    fn dispatch_ms(&self) -> f64 {
        self.dispatch_ms
    }

    fn dispatched_at(&self) -> Instant {
        self.dispatched_at
    }
}

#[derive(Debug, Clone)]
struct SpeechTrace {
    started: Instant,
    api_kind: String,
    target_model: String,
    input_chars: usize,
    stream: bool,
    manager_reference_ms: f64,
    reference_summary: ReferenceSummary,
    target_worker_id: Option<String>,
    priority: Priority,
}

#[derive(Debug, Clone)]
struct ReferenceSummary {
    total: usize,
    metadata: usize,
    inline: usize,
    inline_audio_bytes: usize,
}

fn reference_summary(references: &[InternalReference]) -> ReferenceSummary {
    let metadata = references
        .iter()
        .filter(|reference| reference.voice_id.is_some() && reference.checksum.is_some())
        .count();
    let inline_audio_bytes = references
        .iter()
        .map(|reference| reference.audio_bytes.len())
        .sum();
    ReferenceSummary {
        total: references.len(),
        metadata,
        inline: references.len().saturating_sub(metadata),
        inline_audio_bytes,
    }
}

fn log_speech_completed(
    trace: &SpeechTrace,
    dispatch: &impl DispatchMetrics,
    content_type: &str,
    manager_audio_bytes: u64,
    done: &crate::protocol::InferenceDone,
) {
    tracing::info!(
        api_kind = %trace.api_kind,
        priority = %trace.priority.as_str(),
        request_id = %dispatch.request_id(),
        worker_id = %dispatch.worker_id(),
        stream = trace.stream,
        input_chars = trace.input_chars,
        references = trace.reference_summary.total,
        metadata_references = trace.reference_summary.metadata,
        inline_references = trace.reference_summary.inline,
        inline_audio_bytes = trace.reference_summary.inline_audio_bytes,
        content_type,
        manager_audio_bytes,
        worker_audio_bytes = ?done.audio_bytes,
        worker_chunks = ?done.chunks,
        worker_http_status = ?done.http_status,
        finish_reason = ?done.finish_reason,
        generated_tokens = ?done.generated_tokens,
        max_new_tokens = ?done.max_new_tokens,
        worker_input_characters = ?done.input_characters,
        error_code = ?done.error_code,
        total_ms = elapsed_ms(trace.started),
        manager_reference_ms = trace.manager_reference_ms,
        manager_dispatch_ms = dispatch.dispatch_ms(),
        worker_roundtrip_ms = elapsed_ms(dispatch.dispatched_at()),
        worker_total_ms = ?done.timings.as_ref().map(|timing| timing.total_ms),
        worker_reference_ms = ?done.timings.as_ref().map(|timing| timing.reference_ms),
        worker_sglang_ms = ?done.timings.as_ref().map(|timing| timing.sglang_ms),
        worker_chunk_send_ms = ?done.timings.as_ref().map(|timing| timing.chunk_send_ms),
        worker_first_chunk_ms = ?done.timings.as_ref().and_then(|timing| timing.first_chunk_ms),
        "speech request completed"
    );
}

fn elapsed_ms(started: Instant) -> f64 {
    started.elapsed().as_secs_f64() * 1000.0
}

fn should_retry(
    state: &AppState,
    trace: &SpeechTrace,
    error: &InferenceError,
    no_bytes_sent: bool,
) -> bool {
    if trace.target_worker_id.is_some() {
        return false;
    }

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
    } else if error.code == "tts_out_of_memory" {
        AppError::TtsOutOfMemory(error.message)
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

fn add_tts_completion_headers(
    headers: &mut HeaderMap,
    done: &crate::protocol::InferenceDone,
) -> AppResult<()> {
    fn insert_header(
        headers: &mut HeaderMap,
        name: &'static str,
        value: impl ToString,
    ) -> AppResult<()> {
        let value = HeaderValue::from_str(&value.to_string())
            .map_err(|error| AppError::Internal(anyhow::Error::from(error)))?;
        headers.insert(name, value);
        Ok(())
    }

    if let Some(value) = &done.finish_reason {
        insert_header(headers, "x-tts-finish-reason", value)?;
    }
    if let Some(value) = done.generated_tokens {
        insert_header(headers, "x-tts-generated-tokens", value)?;
    }
    if let Some(value) = done.max_new_tokens {
        insert_header(headers, "x-tts-max-new-tokens", value)?;
    }
    if let Some(value) = done.input_characters {
        insert_header(headers, "x-tts-input-characters", value)?;
    }
    if let Some(value) = &done.error_code {
        insert_header(headers, "x-tts-error-code", value)?;
    }
    Ok(())
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

#[cfg(test)]
mod completion_tests {
    use super::*;
    use crate::protocol::InferenceDone;

    #[test]
    fn truncated_audio_exposes_stable_error_code_header() {
        let done = InferenceDone {
            request_id: "truncated-request".to_string(),
            timings: None,
            audio_bytes: Some(1234),
            chunks: Some(1),
            http_status: Some(206),
            finish_reason: Some("length".to_string()),
            generated_tokens: Some(4096),
            max_new_tokens: Some(4096),
            input_characters: Some(400),
            error_code: Some("tts_output_truncated".to_string()),
        };
        let mut headers = HeaderMap::new();

        add_tts_completion_headers(&mut headers, &done).expect("headers should be valid");

        assert_eq!(
            headers.get("x-tts-error-code").and_then(|value| value.to_str().ok()),
            Some("tts_output_truncated")
        );
    }

    #[test]
    fn normalized_payload_strips_index_params_for_fish_model() {
        let request = AudioSpeechRequest {
            input: "[happy] Hello world".to_string(),
            model: Some("fishaudio/s2-pro".to_string()),
            voice: None,
            ref_audio: Some("data:audio/wav;base64,AAAA".to_string()),
            references: vec![],
            response_format: Some("wav".to_string()),
            stream: Some(false),
            speed: Some(1.2),
            duration_factor: Some(0.8),
            extra_params: Some(IndexExtraParams {
                lang: Some("zhen".to_string()),
                text_normalization: Some(true),
                emo_text: Some("excited".to_string()),
                ..Default::default()
            }),
            emo_audio: Some("base64_data".to_string()),
            emo_audio_base64: None,
            emo_alpha: Some(0.8),
            emo_vector: Some(vec![1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]),
            emo_text: Some("excited".to_string()),
            use_emo_text: Some(true),
            lang: Some("ZH".to_string()),
            text_normalization: Some(true),
            extra: Default::default(),
        };

        let payload = normalized_payload(&request, "fishaudio/s2-pro").expect("should normalize");
        assert_eq!(payload.get("input"), Some(&json!("[happy] Hello world")));
        assert_eq!(payload.get("speed"), Some(&json!(1.2)));
        assert_eq!(payload.get("duration_factor"), None);
        assert_eq!(payload.get("extra_params"), None);
        assert_eq!(payload.get("ref_audio"), None);
        assert_eq!(payload.get("emo_audio"), None);
        assert_eq!(payload.get("emo_alpha"), None);
        assert_eq!(payload.get("emo_vector"), None);
        assert_eq!(payload.get("emo_text"), None);
        assert_eq!(payload.get("use_emo_text"), None);
        assert_eq!(payload.get("lang"), None);
        assert_eq!(payload.get("text_normalization"), None);
    }

    #[test]
    fn normalized_payload_preserves_index_params_for_index_model() {
        let request = AudioSpeechRequest {
            input: "[happy] Hello world".to_string(),
            model: Some("index-tts-2.5".to_string()),
            voice: None,
            ref_audio: None,
            references: vec![],
            response_format: Some("wav".to_string()),
            stream: Some(false),
            speed: Some(1.2),
            duration_factor: Some(0.8),
            extra_params: Some(IndexExtraParams {
                lang: Some("zhen".to_string()),
                text_normalization: Some(true),
                emo_vector: Some(vec![1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]),
                emo_alpha: Some(0.8),
                emo_text: Some("excited".to_string()),
                use_emo_text: Some(true),
                emo_audio: Some("base64_data".to_string()),
                emo_audio_base64: None,
                extra: Default::default(),
            }),
            emo_audio: None,
            emo_audio_base64: None,
            emo_alpha: None,
            emo_vector: None,
            emo_text: None,
            use_emo_text: None,
            lang: None,
            text_normalization: None,
            extra: Default::default(),
        };

        let payload = normalized_payload(&request, "index-tts-2.5").expect("should normalize");
        assert_eq!(payload.get("input"), Some(&json!("[happy] Hello world")));
        assert_eq!(payload.get("speed"), Some(&json!(1.2)));
        assert_eq!(payload.get("duration_factor"), Some(&json!(0.8)));
        assert_eq!(payload.get("lang"), Some(&json!("zhen")));
        assert_eq!(payload.get("text_normalization"), Some(&json!(true)));
        assert_eq!(payload.get("emo_text"), Some(&json!("excited")));
        assert_eq!(payload.get("use_emo_text"), Some(&json!(true)));
        let extra = payload.get("extra_params").and_then(|v| v.as_object()).expect("extra_params object");
        assert_eq!(extra.get("lang"), Some(&json!("zhen")));
        assert_eq!(extra.get("text_normalization"), Some(&json!(true)));
        assert_eq!(extra.get("emo_text"), Some(&json!("excited")));
    }

    #[test]
    fn validate_speech_parameters_rejects_out_of_bound_values() {
        let mut request = AudioSpeechRequest {
            input: "test".to_string(),
            model: None,
            voice: None,
            ref_audio: None,
            references: vec![],
            response_format: None,
            stream: None,
            speed: Some(0.2),
            duration_factor: None,
            extra_params: None,
            emo_audio: None,
            emo_audio_base64: None,
            emo_alpha: None,
            emo_vector: None,
            emo_text: None,
            use_emo_text: None,
            lang: None,
            text_normalization: None,
            extra: Default::default(),
        };
        assert!(validate_speech_parameters(&request).is_err());

        request.speed = Some(1.0);
        request.duration_factor = Some(3.0);
        assert!(validate_speech_parameters(&request).is_err());

        request.duration_factor = Some(1.0);
        request.emo_alpha = Some(-0.1);
        assert!(validate_speech_parameters(&request).is_err());

        request.emo_alpha = Some(0.5);
        request.emo_vector = Some(vec![1.0, 2.0]);
        assert!(validate_speech_parameters(&request).is_err());

        request.emo_vector = Some(vec![0.0; 8]);
        request.lang = Some("INVALID".to_string());
        assert!(validate_speech_parameters(&request).is_err());

        request.lang = Some("zh".to_string());
        assert!(validate_speech_parameters(&request).is_ok());

        request.lang = Some("zhen".to_string());
        assert!(validate_speech_parameters(&request).is_ok());

        // Extra params lang
        request.lang = None;
        request.extra_params = Some(IndexExtraParams {
            lang: Some("zhen".to_string()),
            ..Default::default()
        });
        assert!(validate_speech_parameters(&request).is_ok());
    }
}

async fn admin_get_metrics(
    State(state): State<AppState>,
) -> AppResult<Json<Vec<crate::metrics::MetricsSnapshot>>> {
    let history = state.metrics_store.get_history().await;
    Ok(Json(history))
}
