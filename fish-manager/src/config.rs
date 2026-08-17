use std::{collections::HashMap, env, net::SocketAddr, path::PathBuf, sync::Arc};

use anyhow::{bail, Context};

use crate::protocol::Priority;

#[derive(Clone, Debug)]
pub struct Config {
    pub bind_addr: SocketAddr,
    pub openai_api_keys: Arc<HashMap<String, Priority>>,
    pub worker_token: String,
    pub admin_token: String,
    pub sqlite_path: PathBuf,
    pub blob_local_dir: PathBuf,
    pub retry_on_worker_overload: bool,
    pub max_request_body_bytes: usize,
    pub worker_heartbeat_stale_after_seconds: i64,
    pub default_model: String,
}

impl Config {
    pub fn from_env() -> anyhow::Result<Self> {
        let bind_addr = env::var("MANAGER_BIND_ADDR")
            .unwrap_or_else(|_| "0.0.0.0:8080".to_string())
            .parse()
            .context("MANAGER_BIND_ADDR must be a socket address")?;

        let keys = env::var("OPENAI_API_KEYS").unwrap_or_default();
        let openai_api_keys = parse_openai_api_keys(&keys)?;

        if openai_api_keys.is_empty() {
            bail!("OPENAI_API_KEYS is required, for example OPENAI_API_KEYS=sk-live-1");
        }

        let worker_token = env::var("WORKER_TOKEN").context("WORKER_TOKEN is required")?;
        if worker_token.trim().is_empty() {
            bail!("WORKER_TOKEN must not be empty");
        }

        let admin_token = env::var("ADMIN_TOKEN").unwrap_or_else(|_| "admin".to_string());
        if admin_token.trim().is_empty() {
            bail!("ADMIN_TOKEN must not be empty");
        }

        let sqlite_path = env::var("SQLITE_PATH")
            .map(PathBuf::from)
            .unwrap_or_else(|_| PathBuf::from("./fish-manager-data/manager.sqlite3"));
        let blob_local_dir = env::var("BLOB_LOCAL_DIR")
            .map(PathBuf::from)
            .unwrap_or_else(|_| PathBuf::from("./fish-manager-data/voices"));
        let retry_on_worker_overload = env::var("MANAGER_RETRY_ON_WORKER_OVERLOAD")
            .map(|value| value != "0")
            .unwrap_or(true);
        let max_request_body_bytes = env::var("MAX_REQUEST_BODY_BYTES")
            .ok()
            .and_then(|value| value.parse().ok())
            .unwrap_or(64 * 1024 * 1024);
        let worker_heartbeat_stale_after_seconds = env::var("WORKER_HEARTBEAT_STALE_AFTER_SECONDS")
            .ok()
            .and_then(|value| value.parse().ok())
            .unwrap_or(30);
        let default_model = env::var("DEFAULT_MODEL")
            .unwrap_or_else(|_| "fishaudio/s2-pro".to_string())
            .trim()
            .to_string();

        Ok(Self {
            bind_addr,
            openai_api_keys: Arc::new(openai_api_keys),
            worker_token,
            admin_token,
            sqlite_path,
            blob_local_dir,
            retry_on_worker_overload,
            max_request_body_bytes,
            worker_heartbeat_stale_after_seconds,
            default_model,
        })
    }
}

fn parse_openai_api_keys(keys: &str) -> anyhow::Result<HashMap<String, Priority>> {
    let mut parsed = HashMap::new();

    for raw_entry in keys.split(',') {
        let entry = raw_entry.trim();
        if entry.is_empty() {
            continue;
        }

        let (token, highest_priority) = parse_key_priority_entry(entry)
            .with_context(|| format!("invalid OPENAI_API_KEYS entry '{entry}'"))?;
        if token.is_empty() {
            bail!("OPENAI_API_KEYS contains an empty token");
        }

        parsed.insert(token.to_string(), highest_priority);
    }

    Ok(parsed)
}

fn parse_key_priority_entry(entry: &str) -> anyhow::Result<(&str, Priority)> {
    for delimiter in [':', '='] {
        if let Some((token, priority)) = entry.rsplit_once(delimiter) {
            let priority = priority.trim();
            if priority.starts_with('v') || priority.starts_with('V') {
                let priority = Priority::parse(priority)
                    .ok_or_else(|| anyhow::anyhow!("priority must be one of v1, v2, v3, v4"))?;
                return Ok((token.trim(), priority));
            }
        }
    }

    Ok((entry.trim(), Priority::default()))
}
