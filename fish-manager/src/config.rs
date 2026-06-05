use std::{collections::HashSet, env, net::SocketAddr, path::PathBuf, sync::Arc};

use anyhow::{bail, Context};

#[derive(Clone, Debug)]
pub struct Config {
    pub bind_addr: SocketAddr,
    pub openai_api_keys: Arc<HashSet<String>>,
    pub worker_token: String,
    pub sqlite_path: PathBuf,
    pub blob_local_dir: PathBuf,
    pub retry_on_worker_overload: bool,
}

impl Config {
    pub fn from_env() -> anyhow::Result<Self> {
        let bind_addr = env::var("MANAGER_BIND_ADDR")
            .unwrap_or_else(|_| "0.0.0.0:8080".to_string())
            .parse()
            .context("MANAGER_BIND_ADDR must be a socket address")?;

        let keys = env::var("OPENAI_API_KEYS").unwrap_or_default();
        let openai_api_keys: HashSet<String> = keys
            .split(',')
            .map(str::trim)
            .filter(|key| !key.is_empty())
            .map(ToOwned::to_owned)
            .collect();

        if openai_api_keys.is_empty() {
            bail!("OPENAI_API_KEYS is required, for example OPENAI_API_KEYS=sk-live-1");
        }

        let worker_token = env::var("WORKER_TOKEN").context("WORKER_TOKEN is required")?;
        if worker_token.trim().is_empty() {
            bail!("WORKER_TOKEN must not be empty");
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

        Ok(Self {
            bind_addr,
            openai_api_keys: Arc::new(openai_api_keys),
            worker_token,
            sqlite_path,
            blob_local_dir,
            retry_on_worker_overload,
        })
    }
}
