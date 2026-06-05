use axum::http::{header, HeaderMap};

use crate::{
    config::Config,
    error::{AppError, AppResult},
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

    if config.openai_api_keys.contains(token) {
        Ok(())
    } else {
        Err(AppError::Unauthorized)
    }
}
