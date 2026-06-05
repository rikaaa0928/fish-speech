use axum::{
    http::{header, StatusCode},
    response::{IntoResponse, Response},
    Json,
};
use serde::Serialize;

#[derive(Debug, thiserror::Error)]
pub enum AppError {
    #[error("unauthorized")]
    Unauthorized,
    #[error("forbidden")]
    Forbidden,
    #[error("bad request: {0}")]
    BadRequest(String),
    #[error("not found: {0}")]
    NotFound(String),
    #[error("too many requests: {0}")]
    TooManyRequests(String),
    #[error("upstream error: {0}")]
    Upstream(String),
    #[error(transparent)]
    Internal(#[from] anyhow::Error),
}

#[derive(Serialize)]
struct ErrorBody {
    error: ErrorDetails,
}

#[derive(Serialize)]
struct ErrorDetails {
    message: String,
    #[serde(rename = "type")]
    kind: &'static str,
    code: &'static str,
}

impl IntoResponse for AppError {
    fn into_response(self) -> Response {
        let (status, message, code) = match &self {
            AppError::Unauthorized => (
                StatusCode::UNAUTHORIZED,
                "Invalid authentication credentials".to_string(),
                "invalid_api_key",
            ),
            AppError::Forbidden => (StatusCode::FORBIDDEN, "Forbidden".to_string(), "forbidden"),
            AppError::BadRequest(message) => {
                (StatusCode::BAD_REQUEST, message.clone(), "bad_request")
            }
            AppError::NotFound(message) => (StatusCode::NOT_FOUND, message.clone(), "not_found"),
            AppError::TooManyRequests(message) => (
                StatusCode::TOO_MANY_REQUESTS,
                message.clone(),
                "tts_overloaded",
            ),
            AppError::Upstream(message) => {
                (StatusCode::BAD_GATEWAY, message.clone(), "upstream_error")
            }
            AppError::Internal(error) => {
                tracing::error!(?error, "internal error");
                (
                    StatusCode::INTERNAL_SERVER_ERROR,
                    "Internal server error".to_string(),
                    "internal_error",
                )
            }
        };

        let body = Json(ErrorBody {
            error: ErrorDetails {
                message,
                kind: "invalid_request_error",
                code,
            },
        });

        let mut response = (status, body).into_response();
        if matches!(self, AppError::Unauthorized) {
            response.headers_mut().insert(
                header::WWW_AUTHENTICATE,
                header::HeaderValue::from_static("Bearer"),
            );
        }
        if matches!(self, AppError::TooManyRequests(_)) {
            response
                .headers_mut()
                .insert(header::RETRY_AFTER, header::HeaderValue::from_static("2"));
        }
        response
    }
}

pub type AppResult<T> = Result<T, AppError>;
