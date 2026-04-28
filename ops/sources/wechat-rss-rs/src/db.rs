use rusqlite::{params, Connection, Result};
use std::path::Path;

pub fn init_db<P: AsRef<Path>>(path: P) -> Result<()> {
    let conn = Connection::open(path)?;
    conn.execute_batch(
        "
        CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            biz TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            feed_url TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_refresh_at TEXT,
            last_status TEXT,
            last_error TEXT
        );

        CREATE TABLE IF NOT EXISTS entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subscription_id INTEGER NOT NULL,
            guid TEXT NOT NULL,
            title TEXT NOT NULL,
            link TEXT NOT NULL,
            summary TEXT NOT NULL,
            content_markdown TEXT NOT NULL DEFAULT '',
            published_at TEXT,
            inserted_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            sample_hits INTEGER NOT NULL DEFAULT 0,
            UNIQUE(subscription_id, guid),
            FOREIGN KEY(subscription_id) REFERENCES subscriptions(id)
        );

        CREATE TABLE IF NOT EXISTS fetch_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subscription_id INTEGER NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            sample_fetches INTEGER NOT NULL,
            items_seen INTEGER NOT NULL DEFAULT 0,
            items_saved INTEGER NOT NULL DEFAULT 0,
            note TEXT,
            FOREIGN KEY(subscription_id) REFERENCES subscriptions(id)
        );
    ",
    )?;
    Ok(())
}
