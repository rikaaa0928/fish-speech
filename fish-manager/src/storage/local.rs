use std::path::{Component, Path, PathBuf};

use bytes::Bytes;
use tokio::fs;

use crate::{
    error::{AppError, AppResult},
    storage::{BlobInfo, VoiceBlobStore},
};

pub struct LocalBlobStore {
    root: PathBuf,
}

impl LocalBlobStore {
    pub async fn new(root: impl AsRef<Path>) -> anyhow::Result<Self> {
        let root = root.as_ref().to_path_buf();
        fs::create_dir_all(&root).await?;
        Ok(Self { root })
    }

    fn resolve(&self, key: &str) -> AppResult<PathBuf> {
        let path = Path::new(key);
        if path.components().any(|component| {
            matches!(
                component,
                Component::ParentDir | Component::RootDir | Component::Prefix(_)
            )
        }) {
            return Err(AppError::BadRequest("invalid blob key".to_string()));
        }
        Ok(self.root.join(path))
    }
}

#[async_trait::async_trait]
impl VoiceBlobStore for LocalBlobStore {
    async fn put_audio(&self, key: &str, bytes: Bytes, _content_type: &str) -> AppResult<BlobInfo> {
        let path = self.resolve(key)?;
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent)
                .await
                .map_err(anyhow::Error::from)?;
        }
        let size_bytes = i64::try_from(bytes.len()).unwrap_or(i64::MAX);
        fs::write(&path, bytes).await.map_err(anyhow::Error::from)?;
        Ok(BlobInfo {
            key: key.to_string(),
            size_bytes,
        })
    }

    async fn get_audio(&self, key: &str) -> AppResult<Bytes> {
        let path = self.resolve(key)?;
        let bytes = fs::read(path).await.map_err(anyhow::Error::from)?;
        Ok(Bytes::from(bytes))
    }

    async fn delete_audio(&self, key: &str) -> AppResult<()> {
        let path = self.resolve(key)?;
        match fs::remove_file(path).await {
            Ok(()) => Ok(()),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(error) => Err(anyhow::Error::from(error).into()),
        }
    }
}
