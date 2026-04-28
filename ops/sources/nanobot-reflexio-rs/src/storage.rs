use crate::embedding::{bytes_to_vec, cosine_similarity, vec_to_bytes, SearchResult};
use rusqlite::{params, Connection, Result};
use serde::Serialize;
use std::path::Path;

#[derive(Debug, Serialize, Default)]
pub struct InteractionRecord {
    pub id: i64,
    pub user_id: String,
    pub content: String,
    pub created_at: String,
}

#[derive(Debug, Serialize, Default)]
pub struct FactRecord {
    pub id: i64,
    pub content: String,
    pub created_at: String,
}

pub struct DbStore {
    conn: Connection,
}

impl DbStore {
    pub fn new<P: AsRef<Path>>(path: P) -> Result<Self> {
        let conn = Connection::open(path)?;

        conn.execute(
            "CREATE TABLE IF NOT EXISTS interactions (
                id INTEGER PRIMARY KEY,
                user_id TEXT NOT NULL,
                session_id TEXT,
                content TEXT NOT NULL,
                embedding BLOB,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )",
            [],
        )?;

        conn.execute(
            "CREATE TABLE IF NOT EXISTS facts (
                id INTEGER PRIMARY KEY,
                user_id TEXT NOT NULL,
                content TEXT NOT NULL,
                embedding BLOB,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )",
            [],
        )?;

        // Migrate: add embedding column if missing
        let has_emb_int = conn
            .prepare("SELECT embedding FROM interactions LIMIT 0")
            .is_ok();
        if !has_emb_int {
            let _ = conn.execute("ALTER TABLE interactions ADD COLUMN embedding BLOB", []);
        }
        let has_emb_fact = conn.prepare("SELECT embedding FROM facts LIMIT 0").is_ok();
        if !has_emb_fact {
            let _ = conn.execute("ALTER TABLE facts ADD COLUMN embedding BLOB", []);
        }

        Ok(Self { conn })
    }

    pub fn save_interaction(
        &self,
        user_id: &str,
        session_id: Option<&str>,
        content: &str,
    ) -> Result<i64> {
        self.conn.execute(
            "INSERT INTO interactions (user_id, session_id, content) VALUES (?1, ?2, ?3)",
            params![user_id, session_id, content],
        )?;
        Ok(self.conn.last_insert_rowid())
    }

    pub fn save_fact(&self, user_id: &str, content: &str) -> Result<i64> {
        self.conn.execute(
            "INSERT INTO facts (user_id, content) VALUES (?1, ?2)",
            params![user_id, content],
        )?;
        Ok(self.conn.last_insert_rowid())
    }

    pub fn update_interaction_embedding(&self, id: i64, embedding: &[f32]) -> Result<()> {
        let bytes = vec_to_bytes(embedding);
        self.conn.execute(
            "UPDATE interactions SET embedding = ?1 WHERE id = ?2",
            params![bytes, id],
        )?;
        Ok(())
    }

    pub fn update_fact_embedding(&self, id: i64, embedding: &[f32]) -> Result<()> {
        let bytes = vec_to_bytes(embedding);
        self.conn.execute(
            "UPDATE facts SET embedding = ?1 WHERE id = ?2",
            params![bytes, id],
        )?;
        Ok(())
    }

    pub fn count_interactions(&self) -> Result<i64> {
        Ok(self
            .conn
            .query_row("SELECT COUNT(*) FROM interactions", [], |row| row.get(0))
            .unwrap_or(0))
    }

    pub fn count_facts(&self) -> Result<i64> {
        Ok(self
            .conn
            .query_row("SELECT COUNT(*) FROM facts", [], |row| row.get(0))
            .unwrap_or(0))
    }

    pub fn get_recent_interactions(&self, limit: usize) -> Result<Vec<InteractionRecord>> {
        let mut stmt = self.conn.prepare(
            "SELECT id, user_id, content, strftime(\"%Y-%m-%d %H:%M:%S\", created_at, \"+8 hours\") AS created_at FROM interactions ORDER BY id DESC LIMIT ?"
        )?;
        let rows = stmt.query_map(params![limit], |row| {
            Ok(InteractionRecord {
                id: row.get(0)?,
                user_id: row.get(1)?,
                content: row.get(2)?,
                created_at: row.get(3)?,
            })
        })?;
        let mut results = Vec::new();
        for row in rows {
            results.push(row?);
        }
        Ok(results)
    }

    pub fn get_recent_facts(&self, limit: usize) -> Result<Vec<FactRecord>> {
        let mut stmt = self.conn.prepare(
            "SELECT id, content, strftime(\"%Y-%m-%d %H:%M:%S\", created_at, \"+8 hours\") AS created_at FROM facts ORDER BY id DESC LIMIT ?"
        )?;
        let rows = stmt.query_map(params![limit], |row| {
            Ok(FactRecord {
                id: row.get(0)?,
                content: row.get(1)?,
                created_at: row.get(2)?,
            })
        })?;
        let mut results = Vec::new();
        for row in rows {
            results.push(row?);
        }
        Ok(results)
    }

    pub fn search_similar(
        &self,
        query_embedding: &[f32],
        limit: usize,
        threshold: f32,
    ) -> Result<Vec<SearchResult>> {
        let mut results: Vec<SearchResult> = Vec::new();

        // Search facts
        {
            let mut stmt = self.conn.prepare(
                "SELECT id, content, embedding, strftime(\"%Y-%m-%d %H:%M:%S\", created_at, \"+8 hours\") AS created_at FROM facts WHERE embedding IS NOT NULL"
            )?;
            let rows = stmt.query_map([], |row| {
                let id: i64 = row.get(0)?;
                let content: String = row.get(1)?;
                let emb_bytes: Vec<u8> = row.get(2)?;
                let created_at: String = row.get(3)?;
                Ok((id, content, emb_bytes, created_at))
            })?;
            for row in rows {
                let (id, content, emb_bytes, created_at) = row?;
                let emb = bytes_to_vec(&emb_bytes);
                let score = cosine_similarity(query_embedding, &emb);
                if score >= threshold {
                    results.push(SearchResult {
                        id,
                        score,
                        content,
                        kind: "fact".to_string(),
                        created_at,
                    });
                }
            }
        }

        // Search interactions
        {
            let mut stmt = self.conn.prepare(
                "SELECT id, content, embedding, strftime(\"%Y-%m-%d %H:%M:%S\", created_at, \"+8 hours\") AS created_at FROM interactions WHERE embedding IS NOT NULL"
            )?;
            let rows = stmt.query_map([], |row| {
                let id: i64 = row.get(0)?;
                let content: String = row.get(1)?;
                let emb_bytes: Vec<u8> = row.get(2)?;
                let created_at: String = row.get(3)?;
                Ok((id, content, emb_bytes, created_at))
            })?;
            for row in rows {
                let (id, content, emb_bytes, created_at) = row?;
                let emb = bytes_to_vec(&emb_bytes);
                let score = cosine_similarity(query_embedding, &emb);
                if score >= threshold {
                    results.push(SearchResult {
                        id,
                        score,
                        content,
                        kind: "interaction".to_string(),
                        created_at,
                    });
                }
            }
        }

        results.sort_by(|a, b| {
            b.score
                .partial_cmp(&a.score)
                .unwrap_or(std::cmp::Ordering::Equal)
        });
        results.truncate(limit);
        Ok(results)
    }
}
