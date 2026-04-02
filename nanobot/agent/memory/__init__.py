"""Memory system for persistent agent memory - Vector Enhanced Edition.

Exports:
- MemoryStore: Original file-based memory (MEMORY.md + HISTORY.md)
- EnhancedMemoryStore: Vector-backed memory with semantic search
- VectorMemoryManager: SQLite + embeddings for semantic retrieval
- EmbeddingGenerator: Lightweight embedding model wrapper
- MemoryOptimizer: Four-dimensional self-optimization
"""

import os
from pathlib import Path

# Import original MemoryStore from store.py (migrated from memory.py)
from nanobot.agent.memory.store import MemoryStore, MemoryConsolidator

# Import the new vector components
VECTOR_AVAILABLE = False
EmbeddingGenerator = None
VectorMemoryManager = None
MemoryOptimizer = None
EnhancedMemoryStore = None

try:
    from nanobot.agent.memory.embedding import EmbeddingGenerator
    from nanobot.agent.memory.vector_manager import VectorMemoryManager
    from nanobot.agent.memory.optimizer import MemoryOptimizer

    class EnhancedMemoryStore(MemoryStore):
        """
        Enhanced memory store with SQLite + lightweight embedding.

        Provides backward-compatible API while using the new vector system.
        Falls back to file-based storage if embedding model not available.
        """

        def __init__(self, workspace: Path):
            """Initialize enhanced memory store."""
            super().__init__(workspace)

            # Try to initialize vector manager, fallback to None if model not available
            self._vector_manager = None
            self._optimizer = None
            self._vector_available = False

            try:
                self._vector_manager = VectorMemoryManager(workspace)
                self._optimizer = MemoryOptimizer(self._vector_manager)
                self._vector_available = True
                import loguru
                loguru.logger.info(
                    "EnhancedMemoryStore: vector system initialized | "
                    f"db={workspace / 'memory.db'}"
                )
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning(
                    f"EnhancedMemoryStore: vector memory not available: {e}"
                )

        @property
        def vector_manager(self):
            return self._vector_manager

        @property
        def optimizer(self):
            return self._optimizer

        @property
        def _vector_enabled(self):
            return self._vector_available

        def add_memory(
            self, content: str, category: str = "general", priority: float = 5.0
        ) -> str:
            """Add a memory entry with vector embedding."""
            if self._vector_available:
                memory_id = self._vector_manager.add(content, category, priority)
                if self._optimizer and self._optimizer.record_operation():
                    self._optimizer.light_optimize()
                return memory_id
            else:
                return super().append_long_term(f"[{category}] {content}")

        def search_similar(self, query: str, n_results: int = 5) -> list:
            """Semantic search for similar memories."""
            if self._vector_available:
                return self._vector_manager.search(query, top_k=n_results)
            else:
                # Fallback: return empty list
                return []

        def get_relevant_context(self, query: str, max_memories: int = 3) -> str:
            """Get relevant memory context via semantic search."""
            if not self._vector_available:
                return super().get_memory_context()

            results = self._vector_manager.search(query, top_k=max_memories)
            if not results:
                return super().get_memory_context()

            parts = ["## Relevant Memories (Semantic)\n"]
            for r in results:
                parts.append(
                    f"- [{r['category']}] (relevance: {r['similarity']:.2f}) "
                    f"{r['content']}"
                )
            return "\n".join(parts)

        def get_memory_stats(self) -> dict:
            """Get memory statistics from both systems."""
            if self._vector_available:
                return self._vector_manager.get_stats()
            else:
                return {"status": "fallback_mode"}

        def optimize_memories(self, force: bool = False) -> dict:
            """Run memory optimization."""
            if self._vector_available and self._optimizer:
                return self._optimizer.full_optimize()
            else:
                return {"status": "unavailable"}

    VECTOR_AVAILABLE = True

except ImportError as e:
    EnhancedMemoryStore = MemoryStore
    import logging
    logging.getLogger(__name__).warning(
        f"Vector memory components not available: {e}"
    )


__all__ = [
    "MemoryStore",
    "MemoryConsolidator",
    "EnhancedMemoryStore",
    "EmbeddingGenerator",
    "VectorMemoryManager",
    "MemoryOptimizer",
    "VECTOR_AVAILABLE",
]
