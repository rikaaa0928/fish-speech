mod auth;
mod config;
mod error;
mod protocol;
mod routes;
mod state;
mod storage;
mod workers;

use std::sync::Arc;

use anyhow::Context;
use axum::{extract::DefaultBodyLimit, Router};
use tokio::net::TcpListener;
use tower_http::{cors::CorsLayer, trace::TraceLayer};
use tracing_subscriber::{layer::SubscriberExt, util::SubscriberInitExt};

use crate::{config::Config, routes::build_router, state::AppState, storage::VoiceStore};

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    tracing_subscriber::registry()
        .with(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "fish_manager=info,tower_http=info".into()),
        )
        .with(
            tracing_subscriber::fmt::layer()
                .with_timer(tracing_subscriber::fmt::time::UtcTime::rfc_3339()),
        )
        .init();

    let config = Arc::new(Config::from_env()?);
    let voice_store = Arc::new(VoiceStore::from_config(&config).await?);
    let state = AppState::new(config.clone(), voice_store);

    let app: Router = build_router(state)
        .layer(TraceLayer::new_for_http())
        .layer(DefaultBodyLimit::max(config.max_request_body_bytes))
        .layer(CorsLayer::permissive());

    let listener = TcpListener::bind(config.bind_addr)
        .await
        .with_context(|| format!("failed to bind {}", config.bind_addr))?;

    tracing::info!(addr = %config.bind_addr, "fish-manager listening");
    axum::serve(listener, app).await?;
    Ok(())
}
