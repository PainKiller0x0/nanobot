"""
Thin wrapper: maps EmbeddingGenerator API → ONNXEmbeddingGenerator.
Keeps the same class name so existing code (VectorMemoryManager, etc.)
doesn't need to change.
"""

from __future__ import annotations

import hashlib
import pickle
from pathlib import Path
from typing import Union, List

import numpy as np


class EmbeddingGenerator:
    """
    Drop-in replacement for the old torch-based EmbeddingGenerator.
    Delegates to ONNXEmbeddingGenerator; no torch loaded.
    """

    def __init__(self, cache_dir: Path = None, model_name: str = None):
        _cache = cache_dir or (Path.home() / '.nanobot' / 'embedding_cache')
        _cache.mkdir(parents=True, exist_ok=True)
        self.cache_dir = _cache
        self._onnx_instance = None

    @property
    def onnx(self):
        if self._onnx_instance is None:
            from nanobot.embedding import ONNXEmbeddingGenerator as _ONNXGen
            self._onnx_instance = _ONNXGen(cache_dir=self.cache_dir)
        return self._onnx_instance

    def encode(
        self,
        text: Union[str, List[str]],
        use_cache: bool = True,
        compress: bool = True,
    ) -> np.ndarray:
        """
        Generate embedding for text with optional caching.
        Returns float16 ndarray when compress=True (same as before).
        """
        is_single = isinstance(text, str)
        texts = [text] if is_single else text

        results = []
        to_encode = []
        idx_map = {}

        # Check cache
        if use_cache:
            for i, t in enumerate(texts):
                key = hashlib.md5(t.encode()).hexdigest()
                path = self.cache_dir / f'{key}.pkl'
                if path.exists():
                    with open(path, 'rb') as f:
                        results.append((i, pickle.load(f)))
                else:
                    to_encode.append(t)
                    idx_map[len(to_encode) - 1] = i
        else:
            to_encode = texts
            idx_map = {i: i for i in range(len(texts))}

        # Encode missing
        if to_encode:
            embeddings = self.onnx.encode(to_encode)
            if compress:
                embeddings = embeddings.astype(np.float16)
            for j, emb in enumerate(embeddings):
                results.append((idx_map[j], emb))

                # Save to cache
                if use_cache:
                    t = to_encode[j]
                    key = hashlib.md5(t.encode()).hexdigest()
                    path = self.cache_dir / f'{key}.pkl'
                    with open(path, 'wb') as f:
                        pickle.dump(emb, f, protocol=pickle.HIGHEST_PROTOCOL)

        # Restore original order
        results.sort(key=lambda x: x[0])
        if is_single:
            return results[0][1]
        return np.stack([r[1] for r in results])

    def cosine_similarity(self, a: np.ndarray, b: np.ndarray) -> float:
        return self.onnx.cosine_similarity(a, b)
