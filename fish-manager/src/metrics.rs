use std::{
    collections::{HashMap, VecDeque},
    sync::Arc,
    time::Duration,
};

use chrono::{DateTime, Utc};
use serde::Serialize;
use tokio::sync::RwLock;

use crate::state::AppState;
use crate::protocol::{Priority, PriorityChars, PriorityCounts};

#[derive(Debug, Clone, Serialize)]
pub struct MetricsSnapshot {
    pub timestamp: DateTime<Utc>,
    
    // Globally aggregated metrics
    pub total_tasks: u64,
    pub total_chars: u64,
    
    // Deltas over the interval (usually 1 minute)
    pub tasks_per_min: f64,
    pub chars_per_min: f64,
    
    // Per-worker speed (chars/min excluding idle time)
    // worker_id -> speed
    pub worker_speeds: HashMap<String, f64>,

    // Current workload aggregated across all connected workers.
    pub inflight_by_priority: PriorityCounts,
    pub queued_by_priority: PriorityCounts,
    pub inflight_chars_by_priority: PriorityChars,
    pub queued_chars_by_priority: PriorityChars,
}

pub struct MetricsStore {
    history: RwLock<VecDeque<MetricsSnapshot>>,
    max_history: usize,
}

impl MetricsStore {
    pub fn new(max_history: usize) -> Self {
        Self {
            history: RwLock::new(VecDeque::with_capacity(max_history)),
            max_history,
        }
    }

    pub async fn add_snapshot(&self, snapshot: MetricsSnapshot) {
        let mut hist = self.history.write().await;
        if hist.len() >= self.max_history {
            hist.pop_front();
        }
        hist.push_back(snapshot);
    }

    pub async fn get_history(&self) -> Vec<MetricsSnapshot> {
        let hist = self.history.read().await;
        hist.iter().cloned().collect()
    }
}

pub async fn metrics_ticker(app_state: Arc<AppState>, metrics_store: Arc<MetricsStore>) {
    let mut interval = tokio::time::interval(Duration::from_secs(60));
    
    // Track previous values for deltas
    let mut prev_total_tasks: Option<u64> = None;
    let mut prev_total_chars: Option<u64> = None;
    let mut prev_worker_stats: HashMap<String, (u64, f64)> = HashMap::new(); // worker_id -> (chars, active_time)
    
    loop {
        interval.tick().await;
        
        let mut current_total_tasks = 0;
        let mut current_total_chars = 0;
        let mut current_worker_speeds = HashMap::new();
        let mut inflight_by_priority = PriorityCounts::new();
        let mut queued_by_priority = PriorityCounts::new();
        let mut inflight_chars_by_priority = PriorityChars::new();
        let mut queued_chars_by_priority = PriorityChars::new();
        
        let mut current_worker_stats = HashMap::new();
        
        for entry in app_state.workers.iter() {
            let worker_id = entry.key().clone();
            let worker = entry.value();
            
            let status = worker.status.read().await;
            
            let tasks = status.total_completed_tasks;
            let chars = status.total_completed_chars;
            let active_time = status.total_processing_time_sec;
            
            current_total_tasks += tasks;
            current_total_chars += chars;

            for priority in Priority::ALL {
                *inflight_by_priority.entry(priority).or_default() +=
                    status.inflight_by_priority.get(&priority).copied().unwrap_or(0);
                *queued_by_priority.entry(priority).or_default() +=
                    status.queued_by_priority.get(&priority).copied().unwrap_or(0);
                *inflight_chars_by_priority.entry(priority).or_default() += status
                    .inflight_chars_by_priority
                    .get(&priority)
                    .copied()
                    .unwrap_or(0);
                *queued_chars_by_priority.entry(priority).or_default() += status
                    .queued_chars_by_priority
                    .get(&priority)
                    .copied()
                    .unwrap_or(0);
            }
            
            current_worker_stats.insert(worker_id.clone(), (chars, active_time));
            
            if let Some((prev_chars, prev_active_time)) = prev_worker_stats.get(&worker_id) {
                let char_delta = chars.saturating_sub(*prev_chars);
                let time_delta = active_time - *prev_active_time;
                
                if time_delta > 0.0 {
                    // Speed in chars per minute of active processing time
                    let speed = (char_delta as f64) / (time_delta / 60.0);
                    current_worker_speeds.insert(worker_id, speed);
                } else {
                    current_worker_speeds.insert(worker_id, 0.0);
                }
            } else {
                current_worker_speeds.insert(worker_id, 0.0);
            }
        }
        
        let tasks_per_min = if let Some(prev) = prev_total_tasks {
            current_total_tasks.saturating_sub(prev) as f64
        } else {
            0.0
        };
        
        let chars_per_min = if let Some(prev) = prev_total_chars {
            current_total_chars.saturating_sub(prev) as f64
        } else {
            0.0
        };
        
        prev_total_tasks = Some(current_total_tasks);
        prev_total_chars = Some(current_total_chars);
        prev_worker_stats = current_worker_stats;
        
        let snapshot = MetricsSnapshot {
            timestamp: Utc::now(),
            total_tasks: current_total_tasks,
            total_chars: current_total_chars,
            tasks_per_min,
            chars_per_min,
            worker_speeds: current_worker_speeds,
            inflight_by_priority,
            queued_by_priority,
            inflight_chars_by_priority,
            queued_chars_by_priority,
        };
        
        metrics_store.add_snapshot(snapshot).await;
    }
}
