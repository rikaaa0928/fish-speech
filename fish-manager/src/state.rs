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
    protocol::{Heartbeat, Priority, PriorityChars, PriorityCounts, WireMessage},
    storage::VoiceStore,
};

#[derive(Clone)]
pub struct AppState {
    pub config: Arc<Config>,
    pub voice_store: Arc<VoiceStore>,
    pub workers: Arc<DashMap<String, WorkerHandle>>,
    pub pending: Arc<DashMap<String, PendingRequest>>,
    pub metrics_store: Arc<crate::metrics::MetricsStore>,
}

impl AppState {
    pub fn new(config: Arc<Config>, voice_store: Arc<VoiceStore>, metrics_store: Arc<crate::metrics::MetricsStore>) -> Self {
        Self {
            config,
            voice_store,
            workers: Arc::new(DashMap::new()),
            pending: Arc::new(DashMap::new()),
            metrics_store,
        }
    }

    pub async fn supports_model(&self, model: &str) -> bool {
        for entry in self.workers.iter() {
            let status = entry.value().status.read().await;
            if status.models.iter().any(|m| m.eq_ignore_ascii_case(model)) {
                return true;
            }
        }
        false
    }

    pub async fn resolve_target_model(&self, requested_model: Option<&str>) -> String {
        if let Some(model) = requested_model.map(str::trim).filter(|s| !s.is_empty()) {
            if self.supports_model(model).await {
                return model.to_string();
            }
            tracing::info!(
                requested_model = %model,
                default_model = %self.config.default_model,
                "requested model not found among online workers, falling back to default model"
            );
        }
        self.config.default_model.clone()
    }

    pub async fn select_worker(
        &self,
        target_model: &str,
        exclude: &HashSet<String>,
        priority: Priority,
    ) -> AppResult<WorkerHandle> {
        let mut best: Option<(u32, WorkerHandle)> = None;
        let now = Utc::now();
        let mut total = 0_u32;
        let mut model_mismatch = 0_u32;
        let mut excluded = 0_u32;
        let mut not_ready = 0_u32;
        let mut unhealthy = 0_u32;
        let mut stale = 0_u32;
        let mut candidates = 0_u32;

        for entry in self.workers.iter() {
            total += 1;
            let worker = entry.value().clone();
            let status = worker.status.read().await.clone();

            if !status.models.iter().any(|m| m.eq_ignore_ascii_case(target_model)) {
                model_mismatch += 1;
                continue;
            }

            if exclude.contains(entry.key()) {
                excluded += 1;
                continue;
            }

            let heartbeat_age_seconds = (now - status.last_heartbeat_at).num_seconds();
            if heartbeat_age_seconds > self.config.worker_heartbeat_stale_after_seconds {
                stale += 1;
                continue;
            }
            if !status.ready {
                not_ready += 1;
                continue;
            }
            if !status.sglang_healthy {
                unhealthy += 1;
                continue;
            }

            candidates += 1;
            let manager_inflight = worker.manager_inflight.load(Ordering::Relaxed);
            let effective_inflight = manager_inflight.max(status.inflight);
            let pressure = effective_inflight.saturating_add(status.effective_queued(priority));

            match &best {
                Some((best_pressure, _)) if pressure >= *best_pressure => {}
                _ => best = Some((pressure, worker)),
            }
        }

        best.map(|(_, worker)| worker).ok_or_else(|| {
            tracing::warn!(
                target_model,
                total_workers = total,
                model_mismatch_workers = model_mismatch,
                excluded_workers = excluded,
                not_ready_workers = not_ready,
                unhealthy_workers = unhealthy,
                stale_workers = stale,
                candidate_workers = candidates,
                heartbeat_stale_after_seconds = self.config.worker_heartbeat_stale_after_seconds,
                "no available worker matched selection criteria"
            );
            AppError::TooManyRequests(
                format!("All workers for model '{target_model}' are overloaded or unavailable. Please retry later."),
            )
        })
    }

    pub async fn select_worker_by_id(&self, worker_id: &str) -> AppResult<WorkerHandle> {
        let worker = self
            .workers
            .get(worker_id)
            .map(|entry| entry.value().clone())
            .ok_or_else(|| AppError::NotFound(format!("worker '{worker_id}' not found")))?;

        let status = worker.status.read().await.clone();
        let heartbeat_age_seconds = (Utc::now() - status.last_heartbeat_at).num_seconds();
        if heartbeat_age_seconds > self.config.worker_heartbeat_stale_after_seconds {
            tracing::warn!(
                worker_id,
                connection_id = %worker.connection_id,
                heartbeat_age_seconds,
                heartbeat_stale_after_seconds = self.config.worker_heartbeat_stale_after_seconds,
                "selected worker has stale heartbeat"
            );
            return Err(AppError::TooManyRequests(format!(
                "Selected worker '{worker_id}' heartbeat is stale. Please retry later."
            )));
        }
        if !status.ready || !status.sglang_healthy {
            tracing::warn!(
                worker_id,
                connection_id = %worker.connection_id,
                ready = status.ready,
                sglang_healthy = status.sglang_healthy,
                last_error = ?status.last_error,
                "selected worker is not available"
            );
            return Err(AppError::TooManyRequests(format!(
                "Selected worker '{worker_id}' is unavailable. Please retry later."
            )));
        }

        Ok(worker)
    }
}

#[derive(Clone)]
pub struct WorkerHandle {
    pub worker_id: String,
    pub connection_id: String,
    pub tx: mpsc::Sender<WireMessage>,
    pub status: Arc<RwLock<WorkerStatus>>,
    pub manager_inflight: Arc<AtomicU32>,
}

#[derive(Debug, Clone, Serialize)]
pub struct WorkerStatus {
    pub worker_id: String,
    pub workload_metrics_version: u32,
    pub connection_id: String,
    pub version: String,
    pub model_id: String,
    pub model_revision: Option<String>,
    pub models: Vec<String>,
    pub gpu_name: Option<String>,
    pub gpu_count: u32,
    pub vram_total_mb: Option<u64>,
    pub max_running_requests: u32,
    pub max_queued_requests: u32,
    pub worker_max_inflight: u32,
    pub worker_max_queue: u32,
    pub max_queued_requests_by_priority: PriorityCounts,
    pub sglang_url: String,
    pub ready: bool,
    pub sglang_healthy: bool,
    pub inflight: u32,
    pub queued: u32,
    pub queued_by_priority: PriorityCounts,
    pub inflight_by_priority: PriorityCounts,
    pub queued_chars_by_priority: PriorityChars,
    pub inflight_chars_by_priority: PriorityChars,
    pub vram_used_mb: Option<u64>,
    pub vram_free_mb: Option<u64>,
    pub gpu_utilization_percent: Option<f32>,
    pub ewma_latency_ms: Option<f64>,
    pub last_error: Option<String>,
    pub connected_at: DateTime<Utc>,
    pub last_heartbeat_at: DateTime<Utc>,
    pub heartbeat_age_ms: i64,
    pub manager_inflight: u32,
    pub total_completed_tasks: u64,
    pub total_completed_chars: u64,
    pub total_processing_time_sec: f64,
}

impl WorkerStatus {
    pub fn apply_heartbeat(&mut self, heartbeat: Heartbeat) {
        self.workload_metrics_version = heartbeat.workload_metrics_version;
        self.ready = heartbeat.ready;
        self.sglang_healthy = heartbeat.sglang_healthy;
        self.inflight = heartbeat.inflight;
        self.queued = heartbeat.queued;
        if let Some(models) = heartbeat.models {
            if !models.is_empty() {
                self.models = models;
            }
        }
        self.queued_by_priority = heartbeat.queued_by_priority;
        self.inflight_by_priority = heartbeat.inflight_by_priority;
        self.queued_chars_by_priority = heartbeat.queued_chars_by_priority;
        self.inflight_chars_by_priority = heartbeat.inflight_chars_by_priority;
        self.max_running_requests = heartbeat.max_running_requests;
        self.max_queued_requests = heartbeat.max_queued_requests;
        self.max_queued_requests_by_priority = heartbeat.max_queued_requests_by_priority;
        self.vram_used_mb = heartbeat.vram_used_mb;
        self.vram_free_mb = heartbeat.vram_free_mb;
        self.gpu_utilization_percent = heartbeat.gpu_utilization_percent;
        self.ewma_latency_ms = heartbeat.ewma_latency_ms;
        self.last_error = heartbeat.last_error;
        self.total_completed_tasks = heartbeat.total_completed_tasks;
        self.total_completed_chars = heartbeat.total_completed_chars;
        self.total_processing_time_sec = heartbeat.total_processing_time_sec;
        self.last_heartbeat_at = Utc::now();
        self.heartbeat_age_ms = 0;
    }

    pub fn effective_queued(&self, priority: Priority) -> u32 {
        if self.queued_by_priority.is_empty() {
            return self.queued;
        }

        Priority::ALL
            .iter()
            .copied()
            .filter(|candidate| candidate.index() <= priority.index())
            .map(|candidate| {
                self.queued_by_priority
                    .get(&candidate)
                    .copied()
                    .unwrap_or(0)
            })
            .sum()
    }
}

#[derive(Debug)]
pub enum WorkerEvent {
    Chunk(crate::protocol::InferenceChunk),
    Done(crate::protocol::InferenceDone),
    Error(crate::protocol::InferenceError),
}

#[derive(Clone)]
pub struct PendingRequest {
    pub worker_id: String,
    pub connection_id: String,
    pub tx: mpsc::Sender<WorkerEvent>,
}
