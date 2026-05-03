use crate::embedding::{bytes_to_vec, cosine_similarity, vec_to_bytes, SearchResult};
use rusqlite::{params, Connection, OptionalExtension, Result};
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

#[derive(Debug, Serialize, Default)]
pub struct MemoryRecord {
    pub id: i64,
    pub user_id: String,
    pub category: String,
    pub content: String,
    pub source: String,
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

        conn.execute(
            "CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY,
                user_id TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'note',
                content TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'manual',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )",
            [],
        )?;
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memories_user_id ON memories(user_id)",
            [],
        )?;
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memories_created ON memories(created_at)",
            [],
        )?;

        // Migrate: add embedding column if missing.
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

    pub fn save_memory(
        &self,
        user_id: &str,
        category: &str,
        content: &str,
        source: &str,
    ) -> Result<i64> {
        self.conn.execute(
            "INSERT INTO memories (user_id, category, content, source) VALUES (?1, ?2, ?3, ?4)",
            params![user_id, category, content, source],
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

    pub fn count_memories(&self) -> Result<i64> {
        Ok(self
            .conn
            .query_row("SELECT COUNT(*) FROM memories", [], |row| row.get(0))
            .unwrap_or(0))
    }

    pub fn latest_memory_at(&self) -> Result<Option<String>> {
        self.conn
            .query_row(
                "SELECT strftime('%Y-%m-%d %H:%M:%S', created_at, '+8 hours') FROM memories ORDER BY id DESC LIMIT 1",
                [],
                |row| row.get(0),
            )
            .optional()
    }

    pub fn get_recent_interactions(&self, limit: usize) -> Result<Vec<InteractionRecord>> {
        let mut stmt = self.conn.prepare(
            "SELECT id, user_id, content, strftime('%Y-%m-%d %H:%M:%S', created_at, '+8 hours') AS created_at FROM interactions ORDER BY id DESC LIMIT ?"
        )?;
        let rows = stmt.query_map(params![limit as i64], |row| {
            Ok(InteractionRecord {
                id: row.get(0)?,
                user_id: row.get(1)?,
                content: row.get(2)?,
                created_at: row.get(3)?,
            })
        })?;
        rows.collect()
    }

    pub fn get_recent_facts(&self, limit: usize) -> Result<Vec<FactRecord>> {
        let mut stmt = self.conn.prepare(
            "SELECT id, content, strftime('%Y-%m-%d %H:%M:%S', created_at, '+8 hours') AS created_at FROM facts ORDER BY id DESC LIMIT ?"
        )?;
        let rows = stmt.query_map(params![limit as i64], |row| {
            Ok(FactRecord {
                id: row.get(0)?,
                content: row.get(1)?,
                created_at: row.get(2)?,
            })
        })?;
        rows.collect()
    }

    pub fn get_recent_memories(&self, limit: usize) -> Result<Vec<MemoryRecord>> {
        let mut stmt = self.conn.prepare(
            "SELECT id, user_id, category, content, source, strftime('%Y-%m-%d %H:%M:%S', created_at, '+8 hours') AS created_at FROM memories ORDER BY id DESC LIMIT ?"
        )?;
        let rows = stmt.query_map(params![limit as i64], memory_from_row)?;
        rows.collect()
    }

    pub fn search_memories(&self, query: &str, limit: usize) -> Result<Vec<MemoryRecord>> {
        let query = query.trim();
        if query.is_empty() {
            return self.get_recent_memories(limit);
        }
        let pattern = format!("%{}%", query.replace('%', "\\%").replace('_', "\\_"));
        let mut stmt = self.conn.prepare(
            "SELECT id, user_id, category, content, source, strftime('%Y-%m-%d %H:%M:%S', created_at, '+8 hours') AS created_at
             FROM memories
             WHERE content LIKE ?1 ESCAPE '\\' OR category LIKE ?1 ESCAPE '\\' OR source LIKE ?1 ESCAPE '\\'
             ORDER BY id DESC LIMIT ?2"
        )?;
        let rows = stmt.query_map(params![pattern, limit as i64], memory_from_row)?;
        rows.collect()
    }

    pub fn search_similar(
        &self,
        query_embedding: &[f32],
        limit: usize,
        threshold: f32,
    ) -> Result<Vec<SearchResult>> {
        let mut results: Vec<SearchResult> = Vec::new();

        {
            let mut stmt = self.conn.prepare(
                "SELECT id, content, embedding, strftime('%Y-%m-%d %H:%M:%S', created_at, '+8 hours') AS created_at FROM facts WHERE embedding IS NOT NULL"
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

        {
            let mut stmt = self.conn.prepare(
                "SELECT id, content, embedding, strftime('%Y-%m-%d %H:%M:%S', created_at, '+8 hours') AS created_at FROM interactions WHERE embedding IS NOT NULL"
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

fn memory_from_row(row: &rusqlite::Row<'_>) -> Result<MemoryRecord> {
    Ok(MemoryRecord {
        id: row.get(0)?,
        user_id: row.get(1)?,
        category: row.get(2)?,
        content: row.get(3)?,
        source: row.get(4)?,
        created_at: row.get(5)?,
    })
}
