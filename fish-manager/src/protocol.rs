use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::Value;

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type", content = "data", rename_all = "snake_case")]
pub enum WireMessage {
    WorkerHello(WorkerHello),
    Heartbeat(Heartbeat),
    InferenceRequest(InferenceRequest),
    InferenceChunk(InferenceChunk),
    InferenceDone(InferenceDone),
    InferenceError(InferenceError),
    CancelRequest(CancelRequest),
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct WorkerHello {
    pub worker_id: String,
    pub version: String,
    pub model_id: String,
    pub model_revision: Option<String>,
    pub gpu_name: Option<String>,
    pub gpu_count: u32,
    pub vram_total_mb: Option<u64>,
    pub max_running_requests: u32,
    pub max_queued_requests: u32,
    pub worker_max_inflight: u32,
    pub worker_max_queue: u32,
    pub sglang_url: String,
    pub started_at: DateTime<Utc>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Heartbeat {
    pub worker_id: String,
    pub ready: bool,
    pub sglang_healthy: bool,
    pub inflight: u32,
    pub queued: u32,
    pub max_running_requests: u32,
    pub max_queued_requests: u32,
    pub vram_used_mb: Option<u64>,
    pub vram_free_mb: Option<u64>,
    pub gpu_utilization_percent: Option<f32>,
    pub ewma_latency_ms: Option<f64>,
    pub last_error: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct InternalReference {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub voice_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub checksum: Option<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty", with = "serde_bytes")]
    pub audio_bytes: Vec<u8>,
    pub content_type: String,
    pub text: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct InferenceRequest {
    pub request_id: String,
    pub api_kind: String,
    pub payload: Value,
    pub references: Vec<InternalReference>,
    pub stream: bool,
    pub deadline_ms: Option<u64>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct InferenceChunk {
    pub request_id: String,
    pub seq: u64,
    pub content_type: String,
    #[serde(with = "serde_bytes")]
    pub bytes: Vec<u8>,
    pub is_sse: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct InferenceDone {
    pub request_id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub timings: Option<InferenceTimings>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub audio_bytes: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub chunks: Option<u64>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct InferenceTimings {
    pub total_ms: f64,
    pub reference_ms: f64,
    pub sglang_ms: f64,
    pub chunk_send_ms: f64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub first_chunk_ms: Option<f64>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct InferenceError {
    pub request_id: String,
    pub code: String,
    pub message: String,
    pub retryable: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CancelRequest {
    pub request_id: String,
}
