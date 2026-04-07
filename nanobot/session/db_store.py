"""SQLite storage layer with Tiered Loading (REPAIRED v4 - AGGRESSIVE COUNT LIMIT)."""
from __future__ import annotations
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from loguru import logger

class SessionStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._conn = None
        self._init_db()

    def _init_db(self) -> None:
        try:
            self._conn = sqlite3.connect(str(self.db_path), timeout=10.0)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("CREATE TABLE IF NOT EXISTS sessions (key TEXT PRIMARY KEY, created_at TEXT, updated_at TEXT, metadata TEXT, last_consolidated INTEGER DEFAULT 0)")
            self._conn.execute("CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL, role TEXT, content TEXT, timestamp TEXT, extra TEXT)")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_key ON messages(key)")
        except Exception as e:
            logger.warning("DB init failed: {}", e); self._conn = None

    @property
    def available(self) -> bool: return self._conn is not None

    def load_session(self, key: str) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
        if not self.available: return None
        try:
            cur = self._conn.execute("SELECT created_at, updated_at, metadata, last_consolidated FROM sessions WHERE key = ?", (key,))
            row = cur.fetchone()
            if not row: return None
            created_at, updated_at, metadata_json, last_consolidated = row
            
            # LOAD ALL BUT APPLY AGGRESSIVE LIMITS
            msg_cur = self._conn.execute("SELECT role, content, timestamp, extra FROM messages WHERE key = ? ORDER BY id DESC LIMIT 15", (key,))
            messages = list(reversed(self._parse_rows(msg_cur.fetchall())))
            
            # Additional 24h filter on top of the 15 items (optional, but keeps it fresh)
            now = datetime.now()
            cutoff = now - timedelta(hours=24)
            filtered = []
            for m in messages:
                try:
                    m_time = datetime.fromisoformat(m["timestamp"].replace(" ", "T").split(".")[0])
                    if m_time > cutoff: filtered.append(m)
                except: filtered.append(m)
            
            # Final result: capped at 12 items for near-instant response
            final_messages = filtered[-12:] if len(filtered) > 12 else filtered

            return {"created_at": created_at, "updated_at": updated_at, "metadata": json.loads(metadata_json or "{}"), "last_consolidated": last_consolidated}, final_messages
        except Exception as e:
            logger.error("Load failed: {}", e); return None

    def _parse_rows(self, rows):
        res = []
        for r in rows:
            role, content, timestamp, extra = r
            m = {"role": role, "content": content, "timestamp": timestamp}
            if extra:
                try: m.update(json.loads(extra))
                except: pass
            res.append(m)
        return res

    def save_session(self, key, metadata, messages):
        if not self.available: return False
        try:
            self._conn.execute("BEGIN")
            self._conn.execute("INSERT OR REPLACE INTO sessions VALUES (?, ?, ?, ?, ?)", (key, metadata.get("created_at"), metadata.get("updated_at"), json.dumps(metadata), metadata.get("last_consolidated", 0)))
            self._conn.execute("DELETE FROM messages WHERE key = ?", (key,))
            for msg in messages:
                self._conn.execute("INSERT INTO messages (key, role, content, timestamp, extra) VALUES (?, ?, ?, ?, ?)", 
                    (key, msg.get("role"), msg.get("content"), msg.get("timestamp", datetime.now().isoformat()), json.dumps({k:v for k,v in msg.items() if k not in ("role","content","timestamp")})))
            self._conn.execute("COMMIT")
            return True
        except Exception as e:
            try: self._conn.execute("ROLLBACK")
            except: pass
            logger.error("Save failed: {}", e); return False
