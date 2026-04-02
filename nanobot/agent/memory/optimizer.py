"""Self-optimization system for vector memories."""
import json
import pickle
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Any, Tuple

from loguru import logger


class MemoryOptimizer:
    """
    Four-dimensional self-optimization system:

    - Content optimization: merge duplicates, clean invalid
    - Retrieval optimization: dynamic top-k and threshold
    - Priority optimization: boost high-value memories
    - Storage optimization: cleanup and compression
    """

    def __init__(self, manager):
        self.manager = manager
        self.operation_count = 0
        self.config = {
            "merge_threshold": 0.90,
            "cleanup_days": 180,
            "min_access_for_keep": 1,
            "min_priority_for_keep": 2.0,
            "optimization_interval": 10,
        }

    def record_operation(self) -> bool:
        """Record operation and check if optimization needed."""
        self.operation_count += 1
        return self.operation_count >= self.config["optimization_interval"]

    def find_duplicates(self) -> List[Tuple[str, str, float]]:
        """Find similar memories that might be duplicates."""
        duplicates = []
        threshold = self.config["merge_threshold"]

        # Get all memories with embeddings
        memories = self._get_all_memories_with_embeddings(limit=1000)
        if len(memories) < 2:
            return []

        checked = set()
        for i, mem1 in enumerate(memories):
            for j, mem2 in enumerate(memories):
                if i >= j:
                    continue
                pair = tuple(sorted([mem1["id"], mem2["id"]]))
                if pair in checked:
                    continue
                checked.add(pair)

                sim = self.manager.embedder.cosine_similarity(
                    mem1["embedding"], mem2["embedding"]
                )
                if sim >= threshold:
                    duplicates.append((mem1["id"], mem2["id"], sim))

        duplicates.sort(key=lambda x: x[2], reverse=True)
        return duplicates

    def merge_memories(self, id1: str, id2: str) -> str:
        """Merge two similar memories."""
        mem1 = self.manager.get_by_id(id1)
        mem2 = self.manager.get_by_id(id2)

        if not mem1 or not mem2:
            return None

        # Merge content (longer one as base)
        if len(mem2["content"]) > len(mem1["content"]):
            merged_content = (
                f"{mem2['content']}\n\n[Related]: {mem1['content'][:200]}"
            )
            base_priority = max(mem1["priority"], mem2["priority"])
        else:
            merged_content = (
                f"{mem1['content']}\n\n[Related]: {mem2['content'][:200]}"
            )
            base_priority = max(mem1["priority"], mem2["priority"])

        # Add merged memory with boosted priority
        merged_id = self.manager.add(
            content=merged_content,
            category=mem1["category"],
            priority=min(10.0, base_priority + 0.5),
            metadata={"merged_from": [id1, id2]},
        )

        # Soft delete originals
        self.manager.soft_delete(id1, reason="merged")
        self.manager.soft_delete(id2, reason="merged")

        logger.info(
            f"Merged memories {id1[:8]} and {id2[:8]} into {merged_id[:8]}"
        )
        return merged_id

    def light_optimize(self) -> Dict[str, Any]:
        """Light optimization (run every N operations)."""
        results = {
            "type": "light",
            "duplicates_merged": 0,
            "timestamp": datetime.now().isoformat(),
        }

        # Merge top duplicates (limit to 3 per cycle)
        duplicates = self.find_duplicates()
        for id1, id2, sim in duplicates[:3]:
            if self.merge_memories(id1, id2):
                results["duplicates_merged"] += 1

        self.operation_count = 0
        logger.info(f"Light optimization complete: {results}")
        return results

    def full_optimize(self) -> Dict[str, Any]:
        """Deep optimization (run on schedule)."""
        results = {
            "type": "deep",
            "duplicates_merged": 0,
            "memories_cleaned": 0,
            "timestamp": datetime.now().isoformat(),
        }

        # Deep duplicate merge
        duplicates = self.find_duplicates()
        for id1, id2, sim in duplicates:
            if self.merge_memories(id1, id2):
                results["duplicates_merged"] += 1

        # Cleanup old invalid memories
        cleaned = self._cleanup_invalid()
        results["memories_cleaned"] = cleaned

        # Vacuum database
        with sqlite3.connect(self.manager.db_path) as conn:
            conn.execute("VACUUM")
            conn.commit()

        logger.info(f"Deep optimization complete: {results}")
        return results

    def _cleanup_invalid(self) -> int:
        """Remove memories that are too old and low-priority."""
        days = self.config["cleanup_days"]
        min_priority = self.config["min_priority_for_keep"]
        min_access = self.config["min_access_for_keep"]

        cutoff = (
            datetime.now() - timedelta(days=days)
        ).isoformat()

        with sqlite3.connect(self.manager.db_path) as conn:
            cursor = conn.execute(
                """SELECT COUNT(*) FROM memories
                WHERE is_deleted = 0
                AND created_at < ?
                AND priority < ?
                AND access_count <= ?""",
                (cutoff, min_priority, min_access),
            )
            count = cursor.fetchone()[0]

            if count > 0:
                conn.execute(
                    """UPDATE memories SET is_deleted = 1
                    WHERE is_deleted = 0
                    AND created_at < ?
                    AND priority < ?
                    AND access_count <= ?""",
                    (cutoff, min_priority, min_access),
                )
                conn.commit()

        return count

    def _get_all_memories_with_embeddings(self, limit: int = 1000) -> List[Dict]:
        """Get all memories for optimization analysis."""
        with sqlite3.connect(self.manager.db_path) as conn:
            cursor = conn.execute(
                """SELECT id, content, category, priority, access_count,
                created_at, embedding, metadata
                FROM memories
                WHERE is_deleted = 0
                LIMIT ?""",
                (limit,),
            )

            results = []
            for row in cursor:
                results.append({
                    "id": row[0],
                    "content": row[1],
                    "category": row[2],
                    "priority": row[3],
                    "access_count": row[4],
                    "created_at": row[5],
                    "embedding": pickle.loads(row[6]),
                    "metadata": json.loads(row[7]) if row[7] else {},
                })

            return results
