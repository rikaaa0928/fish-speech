use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::BTreeMap;

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Priority {
    V1,
    V2,
    V3,
    V4,
}

impl Default for Priority {
    fn default() -> Self {
        Self::V3
    }
}

impl Priority {
    pub const ALL: [Self; 4] = [Self::V1, Self::V2, Self::V3, Self::V4];

    pub fn parse(value: &str) -> Option<Self> {
        match value.trim().to_ascii_lowercase().as_str() {
            "v1" => Some(Self::V1),
            "v2" => Some(Self::V2),
            "v3" => Some(Self::V3),
            "v4" => Some(Self::V4),
            _ => None,
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Self::V1 => "v1",
            Self::V2 => "v2",
            Self::V3 => "v3",
            Self::V4 => "v4",
        }
    }

    pub fn index(self) -> usize {
        match self {
            Self::V1 => 0,
            Self::V2 => 1,
            Self::V3 => 2,
            Self::V4 => 3,
        }
    }

    pub fn allows(self, requested: Self) -> bool {
        requested.index() >= self.index()
    }
}

pub type PriorityCounts = BTreeMap<Priority, u32>;
pub type PriorityChars = BTreeMap<Priority, u64>;

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
    RestartApiServer(RestartApiServer),
    RestartWorker(RestartWorker),
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct WorkerHello {
    pub worker_id: String,
    pub version: String,
    pub model_id: String,
    pub model_revision: Option<String>,
    #[serde(default)]
    pub models: Vec<String>,
    pub gpu_name: Option<String>,
    pub gpu_count: u32,
    pub vram_total_mb: Option<u64>,
    pub max_running_requests: u32,
    pub max_queued_requests: u32,
    pub worker_max_inflight: u32,
    pub worker_max_queue: u32,
    #[serde(default)]
    pub max_queued_requests_by_priority: PriorityCounts,
    pub sglang_url: String,
    pub started_at: DateTime<Utc>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Heartbeat {
    pub worker_id: String,
    #[serde(default)]
    pub workload_metrics_version: u32,
    pub ready: bool,
    pub sglang_healthy: bool,
    pub inflight: u32,
    pub queued: u32,
    #[serde(default)]
    pub models: Option<Vec<String>>,
    #[serde(default)]
    pub queued_by_priority: PriorityCounts,
    #[serde(default)]
    pub inflight_by_priority: PriorityCounts,
    #[serde(default)]
    pub queued_chars_by_priority: PriorityChars,
    #[serde(default)]
    pub inflight_chars_by_priority: PriorityChars,
    pub max_running_requests: u32,
    pub max_queued_requests: u32,
    #[serde(default)]
    pub max_queued_requests_by_priority: PriorityCounts,
    pub vram_used_mb: Option<u64>,
    pub vram_free_mb: Option<u64>,
    pub gpu_utilization_percent: Option<f32>,
    pub ewma_latency_ms: Option<f64>,
    pub last_error: Option<String>,
    #[serde(default)]
    pub total_completed_tasks: u64,
    #[serde(default)]
    pub total_completed_chars: u64,
    #[serde(default)]
    pub total_processing_time_sec: f64,
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
    #[serde(default)]
    pub priority: Priority,
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
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub http_status: Option<u16>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub finish_reason: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub generated_tokens: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub max_new_tokens: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub input_characters: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub error_code: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct InferenceTimings {
    #[serde(default)]
    pub total_ms: f64,
    #[serde(default)]
    pub reference_ms: f64,
    #[serde(default)]
    pub sglang_ms: f64,
    #[serde(default)]
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

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RestartApiServer {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub reason: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RestartWorker {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub reason: Option<String>,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn heartbeat_accepts_workers_without_character_metrics() {
        let heartbeat: Heartbeat = serde_json::from_value(serde_json::json!({
            "worker_id": "legacy-worker",
            "ready": true,
            "sglang_healthy": true,
            "inflight": 1,
            "queued": 2,
            "queued_by_priority": { "v3": 2 },
            "max_running_requests": 2,
            "max_queued_requests": 8,
            "max_queued_requests_by_priority": {},
            "vram_used_mb": null,
            "vram_free_mb": null,
            "gpu_utilization_percent": null,
            "ewma_latency_ms": null,
            "last_error": null
        }))
        .expect("legacy heartbeat should deserialize");

        assert!(heartbeat.inflight_by_priority.is_empty());
        assert!(heartbeat.queued_chars_by_priority.is_empty());
        assert!(heartbeat.inflight_chars_by_priority.is_empty());
        assert_eq!(heartbeat.workload_metrics_version, 0);
    }

    #[test]
    fn inference_done_accepts_workers_without_completion_metadata() {
        let done: InferenceDone = serde_json::from_value(serde_json::json!({
            "request_id": "legacy-request",
            "audio_bytes": 1234,
            "chunks": 1
        }))
        .expect("legacy inference_done should deserialize");

        assert_eq!(done.http_status, None);
        assert_eq!(done.finish_reason, None);
        assert_eq!(done.generated_tokens, None);
        assert_eq!(done.max_new_tokens, None);
        assert_eq!(done.input_characters, None);
        assert_eq!(done.error_code, None);
    }

    #[test]
    fn worker_hello_accepts_legacy_worker_without_models() {
        let hello: WorkerHello = serde_json::from_value(serde_json::json!({
            "worker_id": "legacy-worker",
            "version": "0.1.0",
            "model_id": "fishaudio/s2-pro",
            "model_revision": null,
            "gpu_name": "RTX 4090",
            "gpu_count": 1,
            "vram_total_mb": 24564,
            "max_running_requests": 2,
            "max_queued_requests": 4,
            "worker_max_inflight": 2,
            "worker_max_queue": 4,
            "sglang_url": "http://127.0.0.1:8000",
            "started_at": "2026-08-14T00:00:00Z"
        }))
        .expect("legacy worker_hello should deserialize");

        assert_eq!(hello.model_id, "fishaudio/s2-pro");
        assert!(hello.models.is_empty());
    }

    #[test]
    fn worker_hello_accepts_new_worker_with_models() {
        let hello: WorkerHello = serde_json::from_value(serde_json::json!({
            "worker_id": "index-worker-1",
            "version": "0.2.0",
            "model_id": "index-tts-2.5",
            "models": ["index-tts-2.5", "index-tts"],
            "model_revision": null,
            "gpu_name": "RTX 4090",
            "gpu_count": 1,
            "vram_total_mb": 24564,
            "max_running_requests": 2,
            "max_queued_requests": 4,
            "worker_max_inflight": 2,
            "worker_max_queue": 4,
            "sglang_url": "http://127.0.0.1:8000",
            "started_at": "2026-08-14T00:00:00Z"
        }))
        .expect("new worker_hello should deserialize");

        assert_eq!(hello.models, vec!["index-tts-2.5", "index-tts"]);
    }

    #[test]
    fn inference_done_accepts_timings_with_missing_fields() {
        let done: InferenceDone = serde_json::from_value(serde_json::json!({
            "request_id": "req-1",
            "audio_bytes": 1024,
            "chunks": 1,
            "timings": {
                "total_ms": 120.5
            }
        }))
        .expect("inference_done with partial timings should deserialize");

        let timings = done.timings.expect("timings should be present");
        assert_eq!(timings.total_ms, 120.5);
        assert_eq!(timings.reference_ms, 0.0);
        assert_eq!(timings.sglang_ms, 0.0);
        assert_eq!(timings.chunk_send_ms, 0.0);
    }
}
