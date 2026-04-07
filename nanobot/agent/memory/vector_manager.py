"""Optimized Vector Memory Manager (REPAIRED FULL)."""
from __future__ import annotations
import sqlite3, pickle, json, hashlib
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional, Any
from loguru import logger

class VectorMemoryManager:
    def __init__(self, workspace: Path, model_name: str = None):
        self.workspace = workspace
        self.db_path = workspace / "memory.db"
        from nanobot.agent.memory.embedding import EmbeddingGenerator
        self.embedder = EmbeddingGenerator(cache_dir=workspace / ".embedding_cache", model_name=model_name)
        self._init_database()
        # FIXED: Increased top_k to 15 for richer context retrieval
        self.retrieval_params = {"top_k": 15, "similarity_threshold": 0.35}

    def _init_database(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS memories (id TEXT PRIMARY KEY, content TEXT NOT NULL, category TEXT DEFAULT 'general', priority REAL DEFAULT 5.0, access_count INTEGER DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, last_accessed TIMESTAMP DEFAULT CURRENT_TIMESTAMP, embedding BLOB NOT NULL, is_deleted INTEGER DEFAULT 0, metadata TEXT)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_key ON memories(is_deleted)")

    def add(self, content, category="general", priority=5.0, metadata=None):
        mid = hashlib.sha256(f"{content}:{datetime.now().isoformat()}".encode()).hexdigest()[:16]
        emb = pickle.dumps(self.embedder.encode(content))
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("INSERT INTO memories (id, content, category, priority, embedding, metadata) VALUES (?, ?, ?, ?, ?, ?)", (mid, content, category, priority, emb, json.dumps(metadata)))
        return mid

    def search(self, query, top_k=None, threshold=None):
        top_k = top_k or self.retrieval_params["top_k"]
        threshold = threshold or self.retrieval_params["similarity_threshold"]
        q_emb = self.embedder.encode(query)
        results = []
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("SELECT id, content, embedding, metadata FROM memories WHERE is_deleted = 0")
            for row in cursor:
                mid, content, emb_blob, meta = row
                sim = self.embedder.cosine_similarity(q_emb, pickle.loads(emb_blob))
                if sim >= threshold:
                    results.append({"id": mid, "content": content, "similarity": sim, "metadata": json.loads(meta or "{}")})
        results.sort(key=lambda x: x["similarity"], reverse=True)
        return results[:top_k]
