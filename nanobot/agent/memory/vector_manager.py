"""Vector memory manager with SQLite backend and semantic retrieval."""
import sqlite3
import pickle
import json
import hashlib
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional, Any

from loguru import logger

try:
    from nanobot.agent.memory.embedding import EmbeddingGenerator
except ImportError:
    EmbeddingGenerator = None

# Global flag: set to True during consolidation to skip embedding model loading.
# Prevents OOM when consolidation competes for memory with the embedding model.
_CONSOLIDATION_IN_PROGRESS = False


class VectorMemoryManager:
    """
    Lightweight vector memory manager with SQLite backend.

    Architecture:
    - Storage Layer: SQLite with custom schema
    - Embedding Layer: Compressed float16 vectors
    - Retrieval Layer: Cosine similarity with dynamic filtering
    - Management Layer: Standardized CRUD interface
    """

    def __init__(self, workspace: Path, model_name: str = None):
        self.workspace = workspace
        self.db_path = workspace / "memory.db"

        # Initialize embedding generator
        self.embedder = EmbeddingGenerator(
            cache_dir=workspace / ".embedding_cache",
            model_name=model_name,
        )

        # Initialize database
        self._init_database()

        # Retrieval parameters (for self-optimization)
        self.retrieval_params = {
            "top_k": 5,
            "similarity_threshold": 0.35,
            "time_window_days": 365,
        }

        logger.info(f"VectorMemoryManager initialized | db={self.db_path}")

    def _init_database(self):
        """Initialize SQLite database with optimized schema."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    category TEXT DEFAULT 'general',
                    priority REAL DEFAULT 5.0,
                    access_count INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_accessed TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    embedding BLOB NOT NULL,
                    is_deleted INTEGER DEFAULT 0,
                    metadata TEXT
                )
            """)

            # Indexes for fast retrieval
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_priority ON memories(priority DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_category ON memories(category)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_created ON memories(created_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_deleted ON memories(is_deleted)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_accessed ON memories(last_accessed)"
            )

            # Statistics table for self-optimization
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memory_stats (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            conn.commit()

    def _generate_id(self, content: str) -> str:
        """Generate unique ID for memory."""
        return hashlib.sha256(
            f"{content}:{datetime.now().isoformat()}".encode()
        ).hexdigest()[:16]

    def add(
        self,
        content: str,
        category: str = "general",
        priority: float = 5.0,
        metadata: Dict = None,
    ) -> str:
        """Add a memory entry."""
        memory_id = self._generate_id(content)

        # Generate embedding
        embedding = self.embedder.encode(content)
        embedding_blob = pickle.dumps(embedding, protocol=pickle.HIGHEST_PROTOCOL)

        # Insert into database
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT INTO memories
                (id, content, category, priority, embedding, metadata)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    memory_id,
                    content,
                    category,
                    priority,
                    embedding_blob,
                    json.dumps(metadata) if metadata else None,
                ),
            )
            conn.commit()

        logger.debug(f"Added memory [{memory_id[:8]}]: {content[:50]}...")
        return memory_id

    def get_by_id(self, memory_id: str) -> Optional[Dict]:
        """Get a memory by its ID."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """SELECT id, content, category, priority, access_count,
                created_at, last_accessed, embedding, metadata, is_deleted
                FROM memories WHERE id = ?""",
                (memory_id,),
            )
            row = cursor.fetchone()
            if not row:
                return None

            return self._row_to_memory(row)

    def soft_delete(self, memory_id: str, reason: str = None) -> bool:
        """Soft delete a memory."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "UPDATE memories SET is_deleted = 1 WHERE id = ?",
                (memory_id,),
            )
            conn.commit()
            return cursor.rowcount > 0

    def update_priority(self, memory_id: str, priority: float) -> bool:
        """Update memory priority."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "UPDATE memories SET priority = ? WHERE id = ?",
                (priority, memory_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    def search(
        self,
        query: str,
        top_k: int = None,
        threshold: float = None,
        category: str = None,
        _skip_embed: bool = False,
    ) -> List[Dict]:
        """Semantic search using cosine similarity."""
        # Skip embedding model during consolidation to avoid OOM
        if _skip_embed or _CONSOLIDATION_IN_PROGRESS:
            return []

        top_k = top_k or self.retrieval_params["top_k"]
        threshold = threshold or self.retrieval_params["similarity_threshold"]

        # Generate query embedding
        query_embedding = self.embedder.encode(query)

        results = []
        with sqlite3.connect(self.db_path) as conn:
            if category:
                cursor = conn.execute(
                    """SELECT id, content, category, priority, access_count,
                    created_at, embedding, metadata
                    FROM memories WHERE is_deleted = 0 AND category = ?""",
                    (category,),
                )
            else:
                cursor = conn.execute(
                    """SELECT id, content, category, priority, access_count,
                    created_at, embedding, metadata
                    FROM memories WHERE is_deleted = 0"""
                )

            for row in cursor:
                (
                    memory_id,
                    content,
                    cat,
                    priority,
                    access_count,
                    created_at,
                    emb_blob,
                    meta,
                ) = row

                # Deserialize and calculate similarity
                memory_embedding = pickle.loads(emb_blob)
                similarity = self.embedder.cosine_similarity(query_embedding, memory_embedding)

                if similarity >= threshold:
                    results.append(
                        {
                            "id": memory_id,
                            "content": content,
                            "category": cat,
                            "priority": priority,
                            "access_count": access_count,
                            "created_at": created_at,
                            "similarity": similarity,
                            "metadata": json.loads(meta) if meta else {},
                        }
                    )

        # Sort by similarity (with priority boost)
        results.sort(key=lambda x: x["similarity"] + (x["priority"] / 100), reverse=True)

        # Update access count
        selected = results[:top_k]
        if selected:
            with sqlite3.connect(self.db_path) as conn:
                now = datetime.now().isoformat()
                for r in selected:
                    conn.execute(
                        """UPDATE memories SET access_count = access_count + 1,
                        last_accessed = ? WHERE id = ?""",
                        (now, r["id"]),
                    )
                conn.commit()

        return selected

    def get_stats(self) -> Dict[str, Any]:
        """Get memory statistics."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT COUNT(*), AVG(priority), SUM(access_count) FROM memories WHERE is_deleted = 0"
            )
            total, avg_priority, total_accesses = cursor.fetchone()

            cursor = conn.execute(
                "SELECT category, COUNT(*) FROM memories WHERE is_deleted = 0 GROUP BY category"
            )
            categories = {row[0]: row[1] for row in cursor}

            return {
                "total_memories": total or 0,
                "average_priority": round(avg_priority, 2) if avg_priority else 0,
                "total_accesses": total_accesses or 0,
                "categories": categories,
                "retrieval_params": self.retrieval_params,
            }

    def _row_to_memory(self, row: tuple) -> Dict:
        """Convert a database row to a memory dict."""
        (
            memory_id,
            content,
            cat,
            priority,
            access_count,
            created_at,
            last_accessed,
            emb_blob,
            meta,
            is_deleted,
        ) = row
        return {
            "id": memory_id,
            "content": content,
            "category": cat,
            "priority": priority,
            "access_count": access_count,
            "created_at": created_at,
            "last_accessed": last_accessed,
            "embedding": pickle.loads(emb_blob),
            "metadata": json.loads(meta) if meta else {},
            "is_deleted": is_deleted,
        }
