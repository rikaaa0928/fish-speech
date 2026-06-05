use std::{
    collections::HashSet,
    sync::{
        atomic::{AtomicU32, Ordering},
        Arc,
    },
};

use chrono::{DateTime, Utc};
use dashmap::DashMap;
use serde::Serialize;
use tokio::sync::{mpsc, RwLock};

use crate::{
    config::Config,
    error::{AppError, AppResult},
    protocol::{Heartbeat, WireMessage},
    storage::VoiceStore,
};

#[derive(Clone)]
pub struct AppState {
    pub config: Arc<Config>,
    pub voice_store: Arc<VoiceStore>,
    pub workers: Arc<DashMap<String, WorkerHandle>>,
    pub pending: Arc<DashMap<String, PendingRequest>>,
}

impl AppState {
    pub fn new(config: Arc<Config>, voice_store: Arc<VoiceStore>) -> Self {
        Self {
            config,
            voice_store,
            workers: Arc::new(DashMap::new()),
            pending: Arc::new(DashMap::new()),
        }
    }

    pub async fn select_worker(&self, exclude: &HashSet<String>) -> AppResult<WorkerHandle> {
        let mut best: Option<(u32, WorkerHandle)> = None;

        for entry in self.workers.iter() {
            if exclude.contains(entry.key()) {
                continue;
            }

            let worker = entry.value().clone();
            let status = worker.status.read().await.clone();
            if !status.ready || !status.sglang_healthy {
                continue;
            }

            let manager_inflight = worker.manager_inflight.load(Ordering::Relaxed);
            let effective_inflight = manager_inflight.max(status.inflight);
            let pressure = effective_inflight.saturating_add(status.queued);
            let capacity = status
                .worker_max_inflight
                .saturating_add(status.worker_max_queue);

            if capacity == 0 || pressure >= capacity {
                continue;
            }

            match &best {
                Some((best_pressure, _)) if pressure >= *best_pressure => {}
                _ => best = Some((pressure, worker)),
            }
        }

        best.map(|(_, worker)| worker).ok_or_else(|| {
            AppError::TooManyRequests("All workers are busy. Please retry later.".to_string())
        })
    }
}

#[derive(Clone)]
pub struct WorkerHandle {
    pub worker_id: String,
    pub tx: mpsc::Sender<WireMessage>,
    pub status: Arc<RwLock<WorkerStatus>>,
    pub manager_inflight: Arc<AtomicU32>,
}

#[derive(Debug, Clone, Serialize)]
pub struct WorkerStatus {
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
    pub ready: bool,
    pub sglang_healthy: bool,
    pub inflight: u32,
    pub queued: u32,
    pub vram_used_mb: Option<u64>,
    pub vram_free_mb: Option<u64>,
    pub gpu_utilization_percent: Option<f32>,
    pub ewma_latency_ms: Option<f64>,
    pub last_error: Option<String>,
    pub connected_at: DateTime<Utc>,
    pub last_heartbeat_at: DateTime<Utc>,
    pub manager_inflight: u32,
}

impl WorkerStatus {
    pub fn apply_heartbeat(&mut self, heartbeat: Heartbeat) {
        self.ready = heartbeat.ready;
        self.sglang_healthy = heartbeat.sglang_healthy;
        self.inflight = heartbeat.inflight;
        self.queued = heartbeat.queued;
        self.max_running_requests = heartbeat.max_running_requests;
        self.max_queued_requests = heartbeat.max_queued_requests;
        self.vram_used_mb = heartbeat.vram_used_mb;
        self.vram_free_mb = heartbeat.vram_free_mb;
        self.gpu_utilization_percent = heartbeat.gpu_utilization_percent;
        self.ewma_latency_ms = heartbeat.ewma_latency_ms;
        self.last_error = heartbeat.last_error;
        self.last_heartbeat_at = Utc::now();
    }
}

#[derive(Debug)]
pub enum WorkerEvent {
    Chunk(crate::protocol::InferenceChunk),
    Done,
    Error(crate::protocol::InferenceError),
}

#[derive(Clone)]
pub struct PendingRequest {
    pub worker_id: String,
    pub tx: mpsc::Sender<WorkerEvent>,
}
