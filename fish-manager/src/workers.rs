use std::sync::{
    atomic::{AtomicU32, Ordering},
    Arc,
};

use axum::{
    extract::{
        ws::{Message, WebSocket, WebSocketUpgrade},
        Query, State,
    },
    response::Response,
};
use chrono::Utc;
use futures_util::{SinkExt, StreamExt};
use serde::Deserialize;
use tokio::sync::{mpsc, RwLock};
use uuid::Uuid;

use crate::{
    error::{AppError, AppResult},
    protocol::{InferenceError, WireMessage, WorkerHello},
    state::{AppState, WorkerEvent, WorkerHandle, WorkerStatus},
};

#[derive(Debug, Deserialize)]
pub struct WorkerWsQuery {
    token: String,
}

pub async fn worker_ws_handler(
    State(state): State<AppState>,
    Query(query): Query<WorkerWsQuery>,
    ws: WebSocketUpgrade,
) -> AppResult<Response> {
    if query.token != state.config.worker_token {
        return Err(AppError::Forbidden);
    }

    Ok(ws.on_upgrade(move |socket| handle_worker_socket(state, socket)))
}

async fn handle_worker_socket(state: AppState, mut socket: WebSocket) {
    let hello = match receive_hello(&mut socket).await {
        Ok(hello) => hello,
        Err(error) => {
            tracing::warn!(?error, "worker connection rejected before hello");
            return;
        }
    };

    let worker_id = hello.worker_id.clone();
    let version = hello.version.clone();
    let model_id = hello.model_id.clone();
    let model_revision = hello.model_revision.clone();
    let models = if !hello.models.is_empty() {
        hello.models.clone()
    } else if !hello.model_id.trim().is_empty() {
        vec![hello.model_id.clone()]
    } else {
        vec![state.config.default_model.clone()]
    };
    let gpu_name = hello.gpu_name.clone();
    let gpu_count = hello.gpu_count;
    let sglang_url = hello.sglang_url.clone();
    let connection_id = Uuid::new_v4().simple().to_string();
    let (tx, mut rx) = mpsc::channel::<WireMessage>(128);
    let manager_inflight = Arc::new(AtomicU32::new(0));
    let status = Arc::new(RwLock::new(status_from_hello(hello, connection_id.clone(), &state.config.default_model)));
    let handle = WorkerHandle {
        worker_id: worker_id.clone(),
        connection_id: connection_id.clone(),
        tx,
        status,
        manager_inflight: manager_inflight.clone(),
    };

    if let Some(previous) = state.workers.insert(worker_id.clone(), handle.clone()) {
        let failed_pending =
            fail_pending_for_worker(&state, &worker_id, &previous.connection_id).await;
        tracing::warn!(
            worker_id = %worker_id,
            old_connection_id = %previous.connection_id,
            new_connection_id = %connection_id,
            old_manager_inflight = previous.manager_inflight.load(Ordering::Relaxed),
            failed_pending,
            "replaced existing worker connection with same worker_id"
        );
    }
    tracing::info!(
        worker_id = %worker_id,
        connection_id = %connection_id,
        version = %version,
        model_id = %model_id,
        models = ?models,
        model_revision = ?model_revision,
        gpu_name = ?gpu_name,
        gpu_count,
        sglang_url = %sglang_url,
        "worker connected"
    );

    let (mut sender, mut receiver) = socket.split();
    let writer_worker_id = worker_id.clone();
    let writer_connection_id = connection_id.clone();
    let writer = tokio::spawn(async move {
        while let Some(message) = rx.recv().await {
            let bytes = match rmp_serde::to_vec_named(&message) {
                Ok(bytes) => bytes,
                Err(error) => {
                    tracing::error!(
                        ?error,
                        worker_id = %writer_worker_id,
                        connection_id = %writer_connection_id,
                        "failed to encode worker message"
                    );
                    continue;
                }
            };

            if sender.send(Message::Binary(bytes)).await.is_err() {
                break;
            }
        }
    });

    while let Some(message) = receiver.next().await {
        match message {
            Ok(Message::Binary(bytes)) => match rmp_serde::from_slice::<WireMessage>(&bytes) {
                Ok(message) => process_worker_message(&state, &handle, message).await,
                Err(error) => {
                    tracing::warn!(
                        ?error,
                        worker_id = %worker_id,
                        connection_id = %connection_id,
                        "invalid worker message"
                    )
                }
            },
            Ok(Message::Close(_)) => break,
            Ok(Message::Ping(_)) | Ok(Message::Pong(_)) | Ok(Message::Text(_)) => {}
            Err(error) => {
                tracing::warn!(
                    ?error,
                    worker_id = %worker_id,
                    connection_id = %connection_id,
                    "worker websocket error"
                );
                break;
            }
        }
    }

    writer.abort();
    let removed = state
        .workers
        .remove_if(&worker_id, |_, current| {
            current.connection_id == connection_id
        })
        .is_some();
    let failed_pending = fail_pending_for_worker(&state, &worker_id, &connection_id).await;
    if removed {
        tracing::info!(
            worker_id = %worker_id,
            connection_id = %connection_id,
            failed_pending,
            "worker disconnected"
        );
    } else {
        let current_connection_id = state
            .workers
            .get(&worker_id)
            .map(|entry| entry.connection_id.clone());
        tracing::info!(
            worker_id = %worker_id,
            connection_id = %connection_id,
            current_connection_id = ?current_connection_id,
            failed_pending,
            "stale worker connection disconnected without removing current registration"
        );
    }
}

async fn receive_hello(socket: &mut WebSocket) -> AppResult<WorkerHello> {
    let message = socket
        .recv()
        .await
        .ok_or_else(|| AppError::BadRequest("worker disconnected before hello".to_string()))?
        .map_err(|error| AppError::BadRequest(format!("websocket error before hello: {error}")))?;

    let Message::Binary(bytes) = message else {
        return Err(AppError::BadRequest(
            "first worker message must be binary MessagePack".to_string(),
        ));
    };

    match rmp_serde::from_slice::<WireMessage>(&bytes)
        .map_err(|error| AppError::BadRequest(format!("invalid worker hello: {error}")))?
    {
        WireMessage::WorkerHello(hello) => Ok(hello),
        _ => Err(AppError::BadRequest(
            "first worker message must be worker_hello".to_string(),
        )),
    }
}

async fn process_worker_message(state: &AppState, handle: &WorkerHandle, message: WireMessage) {
    match message {
        WireMessage::Heartbeat(heartbeat) => {
            if heartbeat.worker_id != handle.worker_id {
                tracing::warn!(
                    worker_id = %handle.worker_id,
                    connection_id = %handle.connection_id,
                    heartbeat_worker_id = %heartbeat.worker_id,
                    "heartbeat worker_id mismatch"
                );
                return;
            }
            let mut status = handle.status.write().await;
            let previous_ready = status.ready;
            let previous_sglang_healthy = status.sglang_healthy;
            let previous_last_error = status.last_error.clone();
            status.apply_heartbeat(heartbeat);
            status.manager_inflight = handle.manager_inflight.load(Ordering::Relaxed);
            if previous_ready != status.ready
                || previous_sglang_healthy != status.sglang_healthy
                || previous_last_error != status.last_error
            {
                tracing::info!(
                    worker_id = %handle.worker_id,
                    connection_id = %handle.connection_id,
                    ready = status.ready,
                    previous_ready,
                    sglang_healthy = status.sglang_healthy,
                    previous_sglang_healthy,
                    inflight = status.inflight,
                    queued = status.queued,
                    manager_inflight = status.manager_inflight,
                    last_error = ?status.last_error,
                    "worker heartbeat state changed"
                );
            } else {
                tracing::debug!(
                    worker_id = %handle.worker_id,
                    connection_id = %handle.connection_id,
                    ready = status.ready,
                    sglang_healthy = status.sglang_healthy,
                    inflight = status.inflight,
                    queued = status.queued,
                    manager_inflight = status.manager_inflight,
                    "worker heartbeat received"
                );
            }
        }
        WireMessage::InferenceChunk(chunk) => {
            let request_id = chunk.request_id.clone();
            let tx = state.pending.get(&request_id).map(|entry| entry.tx.clone());
            if let Some(tx) = tx {
                let _ = tx.send(WorkerEvent::Chunk(chunk)).await;
            } else {
                tracing::warn!(worker_id = %handle.worker_id, request_id = %request_id, "received chunk for unknown request");
            }
        }
        WireMessage::InferenceDone(done) => {
            let request_id = done.request_id.clone();
            if let Some((_, pending)) = state.pending.remove(&request_id) {
                let _ = pending.tx.send(WorkerEvent::Done(done)).await;
                if pending.worker_id == handle.worker_id
                    && pending.connection_id == handle.connection_id
                {
                    handle.manager_inflight.fetch_sub(1, Ordering::Relaxed);
                }
            } else {
                tracing::warn!(
                    worker_id = %handle.worker_id,
                    connection_id = %handle.connection_id,
                    request_id = %request_id,
                    "received done for unknown request"
                );
            }
        }
        WireMessage::InferenceError(error) => {
            let request_id = error.request_id.clone();
            tracing::warn!(
                worker_id = %handle.worker_id,
                connection_id = %handle.connection_id,
                request_id = %request_id,
                code = %error.code,
                retryable = error.retryable,
                message = %error.message,
                "received worker inference error"
            );
            if let Some((_, pending)) = state.pending.remove(&request_id) {
                let _ = pending.tx.send(WorkerEvent::Error(error)).await;
                if pending.worker_id == handle.worker_id
                    && pending.connection_id == handle.connection_id
                {
                    handle.manager_inflight.fetch_sub(1, Ordering::Relaxed);
                }
            } else {
                tracing::warn!(
                    worker_id = %handle.worker_id,
                    connection_id = %handle.connection_id,
                    request_id = %request_id,
                    "received error for unknown request"
                );
            }
        }
        WireMessage::WorkerHello(_)
        | WireMessage::InferenceRequest(_)
        | WireMessage::CancelRequest(_)
        | WireMessage::RestartApiServer(_)
        | WireMessage::RestartWorker(_) => {
            tracing::warn!(
                worker_id = %handle.worker_id,
                connection_id = %handle.connection_id,
                "unexpected worker message"
            )
        }
    }
}

async fn fail_pending_for_worker(state: &AppState, worker_id: &str, connection_id: &str) -> usize {
    let request_ids: Vec<String> = state
        .pending
        .iter()
        .filter(|entry| entry.worker_id == worker_id && entry.connection_id == connection_id)
        .map(|entry| entry.key().clone())
        .collect();
    let failed_pending = request_ids.len();

    for request_id in request_ids {
        if let Some((_, pending)) = state.pending.remove(&request_id) {
            let _ = pending
                .tx
                .send(WorkerEvent::Error(InferenceError {
                    request_id,
                    code: "worker_disconnected".to_string(),
                    message: "Worker disconnected before inference completed".to_string(),
                    retryable: true,
                }))
                .await;
        }
    }
    failed_pending
}

fn status_from_hello(hello: WorkerHello, connection_id: String, default_model: &str) -> WorkerStatus {
    let now = Utc::now();
    let models = if !hello.models.is_empty() {
        hello.models
    } else if !hello.model_id.trim().is_empty() {
        vec![hello.model_id.clone()]
    } else {
        vec![default_model.to_string()]
    };
    WorkerStatus {
        worker_id: hello.worker_id,
        workload_metrics_version: 0,
        connection_id,
        version: hello.version,
        model_id: hello.model_id,
        model_revision: hello.model_revision,
        models,
        gpu_name: hello.gpu_name,
        gpu_count: hello.gpu_count,
        vram_total_mb: hello.vram_total_mb,
        max_running_requests: hello.max_running_requests,
        max_queued_requests: hello.max_queued_requests,
        worker_max_inflight: hello.worker_max_inflight,
        worker_max_queue: hello.worker_max_queue,
        max_queued_requests_by_priority: hello.max_queued_requests_by_priority,
        sglang_url: hello.sglang_url,
        ready: true,
        sglang_healthy: false,
        inflight: 0,
        queued: 0,
        queued_by_priority: Default::default(),
        inflight_by_priority: Default::default(),
        queued_chars_by_priority: Default::default(),
        inflight_chars_by_priority: Default::default(),
        vram_used_mb: None,
        vram_free_mb: None,
        gpu_utilization_percent: None,
        ewma_latency_ms: None,
        last_error: None,
        connected_at: now,
        last_heartbeat_at: now,
        heartbeat_age_ms: 0,
        manager_inflight: 0,
        total_completed_tasks: 0,
        total_completed_chars: 0,
        total_processing_time_sec: 0.0,
    }
}
