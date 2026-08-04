use axum::http::{header, HeaderMap};

use crate::{
    config::Config,
    error::{AppError, AppResult},
    protocol::Priority,
};

pub fn require_openai_auth(headers: &HeaderMap, config: &Config) -> AppResult<()> {
    let auth = headers
        .get(header::AUTHORIZATION)
        .and_then(|value| value.to_str().ok())
        .ok_or(AppError::Unauthorized)?;

    let token = auth
        .strip_prefix("Bearer ")
        .ok_or(AppError::Unauthorized)?
        .trim();

    if config.openai_api_keys.contains_key(token) {
        Ok(())
    } else {
        Err(AppError::Unauthorized)
    }
}

pub fn require_openai_auth_for_priority(
    headers: &HeaderMap,
    config: &Config,
    requested_priority: Priority,
) -> AppResult<()> {
    let auth = headers
        .get(header::AUTHORIZATION)
        .and_then(|value| value.to_str().ok())
        .ok_or(AppError::Unauthorized)?;

    let token = auth
        .strip_prefix("Bearer ")
        .ok_or(AppError::Unauthorized)?
        .trim();

    let Some(highest_priority) = config.openai_api_keys.get(token) else {
        return Err(AppError::Unauthorized);
    };

    if highest_priority.allows(requested_priority) {
        Ok(())
    } else {
        Err(AppError::Forbidden)
    }
}

pub fn require_worker_auth(headers: &HeaderMap, config: &Config) -> AppResult<()> {
    let auth = headers
        .get(header::AUTHORIZATION)
        .and_then(|value| value.to_str().ok())
        .ok_or(AppError::Unauthorized)?;

    let token = auth
        .strip_prefix("Bearer ")
        .ok_or(AppError::Unauthorized)?
        .trim();

    if token == config.worker_token {
        Ok(())
    } else {
        Err(AppError::Unauthorized)
    }
}

pub fn require_admin_auth(headers: &HeaderMap, config: &Config) -> AppResult<()> {
    let auth = headers
        .get(header::AUTHORIZATION)
        .and_then(|value| value.to_str().ok())
        .ok_or(AppError::Unauthorized)?;

    let token = auth
        .strip_prefix("Bearer ")
        .ok_or(AppError::Unauthorized)?
        .trim();

    if token == config.admin_token {
        Ok(())
    } else {
        Err(AppError::Unauthorized)
    }
}
