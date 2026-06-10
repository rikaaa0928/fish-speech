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
    let gpu_name = hello.gpu_name.clone();
    let gpu_count = hello.gpu_count;
    let sglang_url = hello.sglang_url.clone();
    let (tx, mut rx) = mpsc::channel::<WireMessage>(128);
    let manager_inflight = Arc::new(AtomicU32::new(0));
    let status = Arc::new(RwLock::new(status_from_hello(hello)));
    let handle = WorkerHandle {
        worker_id: worker_id.clone(),
        tx,
        status,
        manager_inflight: manager_inflight.clone(),
    };

    // TODO(ha): add manager instance ID and worker sticky registration.
    state.workers.insert(worker_id.clone(), handle.clone());
    tracing::info!(
        worker_id,
        version,
        model_id,
        model_revision,
        gpu_name,
        gpu_count,
        sglang_url,
        "worker connected"
    );

    let (mut sender, mut receiver) = socket.split();
    let writer_worker_id = worker_id.clone();
    let writer = tokio::spawn(async move {
        while let Some(message) = rx.recv().await {
            let bytes = match rmp_serde::to_vec_named(&message) {
                Ok(bytes) => bytes,
                Err(error) => {
                    tracing::error!(?error, worker_id = %writer_worker_id, "failed to encode worker message");
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
                    tracing::warn!(?error, worker_id = %worker_id, "invalid worker message")
                }
            },
            Ok(Message::Close(_)) => break,
            Ok(Message::Ping(_)) | Ok(Message::Pong(_)) | Ok(Message::Text(_)) => {}
            Err(error) => {
                tracing::warn!(?error, worker_id = %worker_id, "worker websocket error");
                break;
            }
        }
    }

    writer.abort();
    state.workers.remove(&worker_id);
    fail_pending_for_worker(&state, &worker_id).await;
    tracing::info!(worker_id, "worker disconnected");
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
                tracing::warn!(worker_id = %handle.worker_id, heartbeat_worker_id = %heartbeat.worker_id, "heartbeat worker_id mismatch");
                return;
            }
            let mut status = handle.status.write().await;
            status.apply_heartbeat(heartbeat);
            status.manager_inflight = handle.manager_inflight.load(Ordering::Relaxed);
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
            let request_id = done.request_id;
            if let Some((_, pending)) = state.pending.remove(&request_id) {
                let _ = pending.tx.send(WorkerEvent::Done).await;
                if pending.worker_id == handle.worker_id {
                    handle.manager_inflight.fetch_sub(1, Ordering::Relaxed);
                }
            } else {
                tracing::warn!(worker_id = %handle.worker_id, request_id = %request_id, "received done for unknown request");
            }
        }
        WireMessage::InferenceError(error) => {
            let request_id = error.request_id.clone();
            tracing::warn!(
                worker_id = %handle.worker_id,
                request_id = %request_id,
                code = %error.code,
                retryable = error.retryable,
                message = %error.message,
                "received worker inference error"
            );
            if let Some((_, pending)) = state.pending.remove(&request_id) {
                let _ = pending.tx.send(WorkerEvent::Error(error)).await;
                if pending.worker_id == handle.worker_id {
                    handle.manager_inflight.fetch_sub(1, Ordering::Relaxed);
                }
            } else {
                tracing::warn!(worker_id = %handle.worker_id, request_id = %request_id, "received error for unknown request");
            }
        }
        WireMessage::WorkerHello(_)
        | WireMessage::InferenceRequest(_)
        | WireMessage::CancelRequest(_) => {
            tracing::warn!(worker_id = %handle.worker_id, "unexpected worker message")
        }
    }
}

async fn fail_pending_for_worker(state: &AppState, worker_id: &str) {
    let request_ids: Vec<String> = state
        .pending
        .iter()
        .filter(|entry| entry.worker_id == worker_id)
        .map(|entry| entry.key().clone())
        .collect();

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
}

fn status_from_hello(hello: WorkerHello) -> WorkerStatus {
    let now = Utc::now();
    WorkerStatus {
        worker_id: hello.worker_id,
        version: hello.version,
        model_id: hello.model_id,
        model_revision: hello.model_revision,
        gpu_name: hello.gpu_name,
        gpu_count: hello.gpu_count,
        vram_total_mb: hello.vram_total_mb,
        max_running_requests: hello.max_running_requests,
        max_queued_requests: hello.max_queued_requests,
        worker_max_inflight: hello.worker_max_inflight,
        worker_max_queue: hello.worker_max_queue,
        sglang_url: hello.sglang_url,
        ready: true,
        sglang_healthy: false,
        inflight: 0,
        queued: 0,
        vram_used_mb: None,
        vram_free_mb: None,
        gpu_utilization_percent: None,
        ewma_latency_ms: None,
        last_error: None,
        connected_at: now,
        last_heartbeat_at: now,
        manager_inflight: 0,
    }
}
