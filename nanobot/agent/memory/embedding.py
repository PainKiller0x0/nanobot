"""Lightweight embedding generation with local caching and compression."""
import pickle
import hashlib
import os
from pathlib import Path
from typing import Union, List

import numpy as np

SENTENCE_TRANSFORMERS_AVAILABLE = False
try:
    from sentence_transformers import SentenceTransformer
    SENTENCE_TRANSFORMERS_AVAILABLE = True
except ImportError:
    pass


class EmbeddingGenerator:
    """
    Lightweight embedding generator with caching and compression.

    Features:
    - Local model caching to avoid repeated downloads
    - float16 compression to save 50% storage
    - Batch processing for efficiency
    """

    DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"

    def __init__(self, cache_dir: Path = None, model_name: str = None):
        if not SENTENCE_TRANSFORMERS_AVAILABLE:
            raise ImportError(
                "sentence-transformers not installed. "
                "Run: pip install sentence-transformers"
            )

        self.model_name = model_name or self.DEFAULT_MODEL
        self.cache_dir = cache_dir or Path.home() / ".nanobot" / "embedding_cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        # Use Chinese mirror for faster downloads
        os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"

        self._model = None
        self._embedding_dim = None

    @property
    def model(self):
        """Lazy load model from local cache only."""
        if self._model is None:
            # Let SentenceTransformer auto-discover cached model from huggingface hub
            cache_folder = (
                Path.home() / ".cache" / "torch" / "sentence_transformers"
            )
            self._model = SentenceTransformer(
                self.model_name,
                cache_folder=str(cache_folder),
            )
            self._embedding_dim = self._model.get_sentence_embedding_dimension()
        return self._model

    def encode(
        self,
        text: Union[str, List[str]],
        use_cache: bool = True,
        compress: bool = True,
    ) -> np.ndarray:
        """Generate embedding for text with caching and compression."""
        is_single = isinstance(text, str)
        texts = [text] if is_single else text

        results = []
        texts_to_encode = []
        indices_to_encode = []

        # Check cache first
        if use_cache:
            for i, t in enumerate(texts):
                cache_key = hashlib.md5(t.encode()).hexdigest()
                cache_path = self.cache_dir / f"{cache_key}.pkl"
                if cache_path.exists():
                    with open(cache_path, "rb") as f:
                        results.append((i, pickle.load(f)))
                else:
                    texts_to_encode.append(t)
                    indices_to_encode.append(i)

        # Encode missing texts
        if texts_to_encode:
            embeddings = self.model.encode(texts_to_encode, convert_to_numpy=True)
            if compress:
                embeddings = embeddings.astype(np.float16)
            for idx, text_t, emb in zip(indices_to_encode, texts_to_encode, embeddings):
                if use_cache:
                    cache_key = hashlib.md5(text_t.encode()).hexdigest()
                    cache_path = self.cache_dir / f"{cache_key}.pkl"
                    with open(cache_path, "wb") as f:
                        pickle.dump(emb, f)
                results.append((idx, emb))

        # Sort by original index
        results.sort(key=lambda x: x[0])
        embeddings = np.array([r[1] for r in results])

        return embeddings[0] if is_single else embeddings

    def cosine_similarity(self, a: np.ndarray, b: np.ndarray) -> float:
        """Calculate cosine similarity between two vectors."""
        a = a.astype(np.float32) if a.dtype == np.float16 else a
        b = b.astype(np.float32) if b.dtype == np.float16 else b
        return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))
