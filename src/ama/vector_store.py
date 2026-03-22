from __future__ import annotations

from typing import Any
from uuid import uuid4

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from ama.embeddings import hash_embedding
from ama.sanitize import sanitize_text


class CommsGitVectorStore:
    """Qdrant-backed store for comms and Git SQL chunk text."""

    def __init__(
        self,
        collection: str = "ama_context",
        dim: int = 64,
        path: str | None = None,
    ) -> None:
        self.collection = collection
        self.dim = dim
        if path:
            self.client = QdrantClient(path=path)
        else:
            self.client = QdrantClient(":memory:")
        self._ensure_collection()

    def _ensure_collection(self) -> None:
        names = [c.name for c in self.client.get_collections().collections]
        if self.collection not in names:
            self.client.create_collection(
                collection_name=self.collection,
                vectors_config=VectorParams(size=self.dim, distance=Distance.COSINE),
            )

    def upsert_chunk(
        self,
        text: str,
        *,
        source: str,
        kind: str,
        extra: dict[str, Any] | None = None,
    ) -> str:
        point_id = str(uuid4())
        clean = sanitize_text(text)
        vec = hash_embedding(clean, self.dim)
        payload = {"text": clean[:8000], "source": source, "kind": kind, **(extra or {})}
        self.client.upsert(
            collection_name=self.collection,
            points=[PointStruct(id=point_id, vector=vec, payload=payload)],
        )
        return point_id

    def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        vec = hash_embedding(sanitize_text(query), self.dim)
        res = self.client.query_points(
            collection_name=self.collection,
            query=vec,
            limit=limit,
        )
        out: list[dict[str, Any]] = []
        for hit in res.points:
            row = {"score": hit.score, "payload": hit.payload or {}}
            out.append(row)
        return out
