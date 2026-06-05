use std::path::Path;

use chrono::{DateTime, Utc};
use sqlx::{sqlite::SqlitePoolOptions, Row, SqlitePool};
use tokio::fs;

use crate::{
    error::{AppError, AppResult},
    storage::{VoiceMetadata, VoiceMetadataStore, VoicePage},
};

pub struct SqliteMetadataStore {
    pool: SqlitePool,
}

impl SqliteMetadataStore {
    pub async fn connect(path: &Path) -> anyhow::Result<Self> {
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent).await?;
        }

        let url = format!("sqlite://{}", path.display());
        let pool = SqlitePoolOptions::new()
            .max_connections(5)
            .connect_with(
                url.parse::<sqlx::sqlite::SqliteConnectOptions>()?
                    .create_if_missing(true),
            )
            .await?;

        sqlx::query(
            r#"
            CREATE TABLE IF NOT EXISTS voices (
                voice_id TEXT PRIMARY KEY,
                text TEXT NOT NULL,
                blob_key TEXT NOT NULL,
                content_type TEXT NOT NULL,
                checksum TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
            "#,
        )
        .execute(&pool)
        .await?;

        Ok(Self { pool })
    }
}

#[async_trait::async_trait]
impl VoiceMetadataStore for SqliteMetadataStore {
    async fn create_voice(&self, voice: VoiceMetadata) -> AppResult<()> {
        sqlx::query(
            r#"
            INSERT INTO voices (voice_id, text, blob_key, content_type, checksum, size_bytes, created_at)
            VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)
            "#,
        )
        .bind(&voice.voice_id)
        .bind(&voice.text)
        .bind(&voice.blob_key)
        .bind(&voice.content_type)
        .bind(&voice.checksum)
        .bind(voice.size_bytes)
        .bind(voice.created_at.to_rfc3339())
        .execute(&self.pool)
        .await
        .map_err(|error| {
            if let sqlx::Error::Database(db_error) = &error {
                if db_error.is_unique_violation() {
                    return AppError::BadRequest("voice_id already exists".to_string());
                }
            }
            anyhow::Error::from(error).into()
        })?;
        Ok(())
    }

    async fn get_voice(&self, voice_id: &str) -> AppResult<Option<VoiceMetadata>> {
        let row = sqlx::query(
            r#"
            SELECT voice_id, text, blob_key, content_type, checksum, size_bytes, created_at
            FROM voices
            WHERE voice_id = ?1
            "#,
        )
        .bind(voice_id)
        .fetch_optional(&self.pool)
        .await
        .map_err(anyhow::Error::from)?;

        row.map(row_to_voice).transpose()
    }

    async fn list_voices(&self, cursor: Option<String>, limit: u32) -> AppResult<VoicePage> {
        let limit = limit.clamp(1, 100);
        let rows = if let Some(cursor) = cursor {
            sqlx::query(
                r#"
                SELECT voice_id, text, blob_key, content_type, checksum, size_bytes, created_at
                FROM voices
                WHERE voice_id > ?1
                ORDER BY voice_id ASC
                LIMIT ?2
                "#,
            )
            .bind(cursor)
            .bind(i64::from(limit) + 1)
            .fetch_all(&self.pool)
            .await
        } else {
            sqlx::query(
                r#"
                SELECT voice_id, text, blob_key, content_type, checksum, size_bytes, created_at
                FROM voices
                ORDER BY voice_id ASC
                LIMIT ?1
                "#,
            )
            .bind(i64::from(limit) + 1)
            .fetch_all(&self.pool)
            .await
        }
        .map_err(anyhow::Error::from)?;

        let mut voices = Vec::with_capacity(rows.len());
        for row in rows {
            voices.push(row_to_voice(row)?);
        }

        let next_cursor = if voices.len() > limit as usize {
            let extra = voices.pop().expect("extra voice exists");
            Some(extra.voice_id)
        } else {
            None
        };

        Ok(VoicePage {
            voices,
            next_cursor,
        })
    }

    async fn delete_voice(&self, voice_id: &str) -> AppResult<()> {
        sqlx::query("DELETE FROM voices WHERE voice_id = ?1")
            .bind(voice_id)
            .execute(&self.pool)
            .await
            .map_err(anyhow::Error::from)?;
        Ok(())
    }
}

fn row_to_voice(row: sqlx::sqlite::SqliteRow) -> AppResult<VoiceMetadata> {
    let created_at: String = row.get("created_at");
    let created_at = DateTime::parse_from_rfc3339(&created_at)
        .map_err(|_| AppError::Internal(anyhow::anyhow!("invalid created_at in database")))?
        .with_timezone(&Utc);

    Ok(VoiceMetadata {
        voice_id: row.get("voice_id"),
        text: row.get("text"),
        blob_key: row.get("blob_key"),
        content_type: row.get("content_type"),
        checksum: row.get("checksum"),
        size_bytes: row.get("size_bytes"),
        created_at,
    })
}
