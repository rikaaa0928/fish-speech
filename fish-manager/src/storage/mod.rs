mod local;
mod sqlite;

use std::sync::Arc;

use async_trait::async_trait;
use base64::{engine::general_purpose::STANDARD, Engine as _};
use bytes::Bytes;
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use uuid::Uuid;

use crate::{
    config::Config,
    error::{AppError, AppResult},
};

pub use local::LocalBlobStore;
pub use sqlite::SqliteMetadataStore;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct VoiceMetadata {
    pub voice_id: String,
    pub text: String,
    pub blob_key: String,
    pub content_type: String,
    pub checksum: String,
    pub size_bytes: i64,
    pub created_at: DateTime<Utc>,
}

#[derive(Debug, Clone, Serialize)]
pub struct VoicePage {
    pub voices: Vec<VoiceMetadata>,
    pub next_cursor: Option<String>,
}

#[derive(Debug, Clone)]
pub struct BlobInfo {
    pub key: String,
    pub size_bytes: i64,
}

#[async_trait]
pub trait VoiceMetadataStore: Send + Sync {
    async fn create_voice(&self, voice: VoiceMetadata) -> AppResult<()>;
    async fn get_voice(&self, voice_id: &str) -> AppResult<Option<VoiceMetadata>>;
    async fn list_voices(&self, cursor: Option<String>, limit: u32) -> AppResult<VoicePage>;
    async fn delete_voice(&self, voice_id: &str) -> AppResult<()>;
}

#[async_trait]
pub trait VoiceBlobStore: Send + Sync {
    async fn put_audio(&self, key: &str, bytes: Bytes, content_type: &str) -> AppResult<BlobInfo>;
    async fn get_audio(&self, key: &str) -> AppResult<Bytes>;
    async fn delete_audio(&self, key: &str) -> AppResult<()>;
}

#[allow(dead_code)]
#[async_trait]
pub trait StorageHealth: Send + Sync {
    async fn health_check(&self) -> AppResult<()>;
}

pub struct VoiceStore {
    metadata: Arc<dyn VoiceMetadataStore>,
    blobs: Arc<dyn VoiceBlobStore>,
}

impl VoiceStore {
    pub async fn from_config(config: &Config) -> anyhow::Result<Self> {
        // TODO(ha): replace local SQLite metadata with Postgres for multi-manager deployments.
        // TODO(ha): replace local blob storage with S3/MinIO for multi-manager deployments.
        let metadata = Arc::new(SqliteMetadataStore::connect(&config.sqlite_path).await?);
        let blobs = Arc::new(LocalBlobStore::new(&config.blob_local_dir).await?);
        Ok(Self { metadata, blobs })
    }

    pub async fn create_voice(
        &self,
        voice_id: Option<String>,
        text: String,
        audio_base64: String,
        content_type: Option<String>,
    ) -> AppResult<VoiceMetadata> {
        let audio = decode_audio_base64(&audio_base64)?;
        if audio.is_empty() {
            return Err(AppError::BadRequest(
                "audio_base64 must not be empty".to_string(),
            ));
        }

        let content_type = content_type.unwrap_or_else(|| "audio/wav".to_string());
        let voice_id = voice_id.unwrap_or_else(|| format!("voice_{}", Uuid::new_v4().simple()));
        let checksum = hex_sha256(&audio);
        let key = blob_key(&voice_id, &checksum, &content_type);
        let blob = self
            .blobs
            .put_audio(&key, Bytes::from(audio), &content_type)
            .await?;

        let metadata = VoiceMetadata {
            voice_id,
            text,
            blob_key: blob.key,
            content_type,
            checksum,
            size_bytes: blob.size_bytes,
            created_at: Utc::now(),
        };

        // TODO(ha): add transactional cleanup between metadata and blob stores.
        self.metadata.create_voice(metadata.clone()).await?;
        Ok(metadata)
    }

    pub async fn get_voice(&self, voice_id: &str) -> AppResult<Option<VoiceMetadata>> {
        self.metadata.get_voice(voice_id).await
    }

    pub async fn get_voice_audio(&self, voice: &VoiceMetadata) -> AppResult<Bytes> {
        self.blobs.get_audio(&voice.blob_key).await
    }

    pub async fn list_voices(&self, cursor: Option<String>, limit: u32) -> AppResult<VoicePage> {
        self.metadata.list_voices(cursor, limit).await
    }

    pub async fn delete_voice(&self, voice_id: &str) -> AppResult<()> {
        if let Some(voice) = self.metadata.get_voice(voice_id).await? {
            self.metadata.delete_voice(voice_id).await?;
            self.blobs.delete_audio(&voice.blob_key).await?;
        }
        Ok(())
    }
}

fn decode_audio_base64(input: &str) -> AppResult<Vec<u8>> {
    let payload = input
        .split_once(',')
        .map(|(_, payload)| payload)
        .unwrap_or(input);

    STANDARD
        .decode(payload)
        .map_err(|_| AppError::BadRequest("audio_base64 is not valid base64".to_string()))
}

fn hex_sha256(bytes: &[u8]) -> String {
    let digest = Sha256::digest(bytes);
    digest.iter().map(|byte| format!("{byte:02x}")).collect()
}

fn blob_key(voice_id: &str, checksum: &str, content_type: &str) -> String {
    let prefix = &checksum[..2];
    let ext = match content_type {
        "audio/mpeg" | "audio/mp3" => "mp3",
        "audio/flac" => "flac",
        "audio/ogg" => "ogg",
        "audio/wav" | "audio/x-wav" | _ => "wav",
    };
    format!("{prefix}/{voice_id}_{checksum}.{ext}")
}
