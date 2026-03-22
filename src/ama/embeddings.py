from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    pass


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity for L2-normalized vectors (hash_embedding output)."""
    if len(a) != len(b):
        return 0.0
    return float(np.dot(np.asarray(a), np.asarray(b)))


def hash_embedding(text: str, dim: int = 64) -> list[float]:
    """
    Deterministic pseudo-embedding for offline MVP (no model download).
    Not semantically meaningful — use for plumbing tests; swap for sentence-transformers in production.
    """
    h = hashlib.sha256(text.encode("utf-8")).digest()
    rng = np.random.Generator(np.random.PCG64(int.from_bytes(h[:8], "little")))
    v = rng.standard_normal(dim)
    n = np.linalg.norm(v) or 1.0
    return (v / n).tolist()
