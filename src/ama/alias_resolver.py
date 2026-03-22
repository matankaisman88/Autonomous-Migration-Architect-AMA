from __future__ import annotations

import json
from dataclasses import dataclass, field
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from ama.embeddings import cosine_similarity, hash_embedding
from ama.sanitize import (
    has_rtl_script,
    is_generic_low_signal_name,
    normalize_sql_identifier,
    sanitize_text,
)
from ama.sql_pipeline import ColumnStats, TableColumnStats


HITL_THRESHOLD = 0.8
DEFAULT_MERGE_FLOOR = 0.4
DEFAULT_CONFIRMED_THRESHOLD = 0.8
_MAX_PAIRWISE_DDL = 256
_AMBIGUOUS_TOKENS = frozenset(
    {
        "id",
        "key",
        "code",
        "name",
        "type",
        "status",
        "date",
        "ts",
        "dt",
        "num",
        "no",
        "ref",
    }
)


class LLMReasoner(Protocol):
    """Optional LLM bridge: return (confidence 0..1, citation) or None to abstain."""

    def reason(
        self,
        *,
        log_column: str,
        ddl_column: str,
        vector_similarity: float,
        glossary_hit: bool,
        co_occurring_columns: tuple[str, ...] | None = None,
        ddl_column_list: tuple[str, ...] | None = None,
    ) -> tuple[float, str] | None: ...


@dataclass
class MergeCandidate:
    log_column: str
    ddl_column: str
    merge_confidence: float
    hitl: bool
    citation: str
    strategy: str
    vector_similarity: float


@dataclass
class MergedEntity:
    """canonical_column is the target DDL name — primary key for all downstream reporting."""

    canonical_column: str
    source_columns: list[str]
    combined_stats: ColumnStats
    merge_confidence: float
    hitl: bool
    citations: list[str] = field(default_factory=list)
    strategies: list[str] = field(default_factory=list)
    source_table: str = ""


@dataclass
class UnmappedCandidate:
    """Log column that was not merged onto DDL (review band or trash / sanity fail)."""

    legacy_name: str
    suggested_ddl: str
    merge_confidence: float
    category: str  # "review" | "trash"
    citation: str
    strategy: str
    stats: ColumnStats
    source_table: str = ""


@dataclass
class MergeResult:
    merged_stats: TableColumnStats
    unmapped_stats: TableColumnStats
    confirmed_entities: list[MergedEntity]
    review_candidates: list[UnmappedCandidate]
    trash_candidates: list[UnmappedCandidate]
    proposals: list[MergeCandidate]


def load_glossary(*paths: Path | None) -> dict[str, str]:
    """
    Merge one or more glossary JSON objects (flat string→string maps).
    Later files only add keys not already present (first file wins).
    Keys starting with ``_`` are skipped (reserved for metadata).
    If no path exists or is given, returns :func:`default_glossary`.
    """
    out: dict[str, str] = {}
    any_loaded = False
    for path in paths:
        if path is None or not path.exists():
            continue
        any_loaded = True
        data = json.loads(path.read_text(encoding="utf-8"))
        for k, v in data.items():
            if not isinstance(k, str) or not isinstance(v, str):
                continue
            if k.startswith("_"):
                continue
            nk = normalize_sql_identifier(k)
            nv = normalize_sql_identifier(v)
            if nk and nk not in out:
                out[nk] = nv
    if not any_loaded:
        return default_glossary()
    return out


def default_glossary() -> dict[str, str]:
    """Seed bilingual mappings (extend via JSON). NFC-normalized keys."""
    return {
        normalize_sql_identifier("סטטוס"): "status",
        normalize_sql_identifier("מזהה_לקוח"): "customer_id",
        normalize_sql_identifier("סכום"): "amount",
        normalize_sql_identifier("תאריך_יצירה"): "created_at",
    }


def _ascii_ratio(s: str) -> float:
    if not s:
        return 0.0
    return sum(1 for c in s if ord(c) < 128) / len(s)


def _co_peer_boost(ddl_col: str, peers: frozenset[str]) -> float:
    if not peers:
        return 0.0
    dlow = (ddl_col or "").lower()
    for p in peers:
        pl = (normalize_sql_identifier(p) or p).lower()
        if not pl:
            continue
        if pl in dlow or dlow in pl:
            return 0.14
    return 0.0


def _is_ambiguous_token(log_s: str) -> bool:
    n = (normalize_sql_identifier(log_s) or log_s).lower()
    return len(n) <= 2 or n in _AMBIGUOUS_TOKENS


def _lexical_similarity(a: str, b: str) -> float:
    na, nb = normalize_sql_identifier(a), normalize_sql_identifier(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    if _ascii_ratio(na) > 0.9 and _ascii_ratio(nb) > 0.9:
        return SequenceMatcher(None, na, nb).ratio()
    return 0.0


class AliasColumnVectorStore:
    """Vector index over DDL column names for coarse retrieval (Latin-friendly)."""

    def __init__(
        self,
        collection: str = "ama_alias_columns",
        dim: int = 64,
        path: str | None = None,
    ) -> None:
        self.collection = collection
        self.dim = dim
        self.client = QdrantClient(path or ":memory:")
        self._ensure_collection()

    def _ensure_collection(self) -> None:
        names = [c.name for c in self.client.get_collections().collections]
        if self.collection not in names:
            self.client.create_collection(
                collection_name=self.collection,
                vectors_config=VectorParams(size=self.dim, distance=Distance.COSINE),
            )

    def clear_and_index_ddl(self, ddl_columns: list[str]) -> None:
        try:
            self.client.delete_collection(self.collection)
        except Exception:
            pass
        self._ensure_collection()
        for c in ddl_columns:
            canon = normalize_sql_identifier(c)
            if not canon:
                continue
            text = sanitize_text(f"DDL column canonical identifier: {canon}")
            vec = hash_embedding(text, self.dim)
            pid = str(uuid4())
            self.client.upsert(
                collection_name=self.collection,
                points=[
                    PointStruct(
                        id=pid,
                        vector=vec,
                        payload={"kind": "ddl", "canonical": canon},
                    )
                ],
            )

    def search_log_column(self, log_column: str, limit: int = 5) -> list[tuple[str, float]]:
        raw = sanitize_text(f"log column identifier observed: {log_column}")
        vec = hash_embedding(raw, self.dim)
        res = self.client.query_points(
            collection_name=self.collection,
            query=vec,
            limit=limit,
            with_payload=True,
        )
        out: list[tuple[str, float]] = []
        for hit in res.points:
            pl = hit.payload or {}
            canon = pl.get("canonical")
            if isinstance(canon, str):
                out.append((canon, float(hit.score or 0.0)))
        return out


class AliasResolver:
    """
    Merge log-discovered column names onto DDL canonical names.
    Uses: glossary (high confidence), vector retrieval, lexical similarity, optional LLM.
    """

    def __init__(
        self,
        *,
        ddl_columns: list[str],
        glossary: dict[str, str] | None = None,
        vector_store: AliasColumnVectorStore | None = None,
        llm: LLMReasoner | None = None,
        merge_floor: float = DEFAULT_MERGE_FLOOR,
        confirmed_threshold: float = DEFAULT_CONFIRMED_THRESHOLD,
    ) -> None:
        self.ddl_columns = [normalize_sql_identifier(c) for c in ddl_columns if c.strip()]
        self.glossary = glossary or default_glossary()
        self._llm = llm
        self.merge_floor = merge_floor
        self.confirmed_threshold = confirmed_threshold
        self._store = vector_store or AliasColumnVectorStore()
        if self.ddl_columns:
            self._store.clear_and_index_ddl(self.ddl_columns)

    def _pairwise_ddl_scores(self, log_s: str) -> list[tuple[str, float, float, float]]:
        """
        For each DDL column: (name, vector_sim, lexical_sim, blended_score).
        Blended score ranks candidates — fixes exact English matches (e.g. status) losing to unrelated DDL.
        """
        out: list[tuple[str, float, float, float]] = []
        for d in self.ddl_columns:
            va = hash_embedding(sanitize_text(f"log column identifier: {log_s}"))
            vb = hash_embedding(sanitize_text(f"DDL column canonical: {d}"))
            v = cosine_similarity(va, vb)
            lx = _lexical_similarity(log_s, d)
            blended = min(1.0, 0.55 * v + 0.45 * lx)
            out.append((d, v, lx, blended))
        out.sort(key=lambda x: -x[3])
        return out

    def _glossary_lookup(self, log_col: str) -> str | None:
        key = normalize_sql_identifier(log_col)
        if key in self.glossary:
            return self.glossary[key]
        return None

    def _exact_ddl_match(self, log_s: str) -> str | None:
        ln = normalize_sql_identifier(log_s)
        for d in self.ddl_columns:
            if ln == normalize_sql_identifier(d):
                return d
        return None

    def _sanity_vector_merge(self, log_s: str, mc: MergeCandidate) -> bool:
        """
        Block Hebrew/Arabic → arbitrary English DDL from vector noise unless glossary or exact match.
        If glossary defines a different target than the vector pick, reject the vector path (glossary wins in propose_merge).
        """
        gl = self._glossary_lookup(log_s)
        if gl and gl in self.ddl_columns and mc.ddl_column != gl:
            return False
        if has_rtl_script(log_s) and gl is None and self._exact_ddl_match(log_s) is None:
            return False
        return True

    def _merge_allowed_to_ddl(self, raw_col: str, mc: MergeCandidate) -> bool:
        log_s = sanitize_text(raw_col)
        gl = self._glossary_lookup(log_s)
        if gl and gl in self.ddl_columns:
            return True
        if self._exact_ddl_match(log_s):
            return True
        if not self._sanity_vector_merge(log_s, mc):
            return False
        if mc.merge_confidence < self.merge_floor:
            return False
        if mc.merge_confidence < self.confirmed_threshold:
            return False
        return True

    def _unmapped_category(self, mc: MergeCandidate) -> str:
        if mc.merge_confidence < self.merge_floor or not self._sanity_vector_merge(
            sanitize_text(mc.log_column), mc
        ):
            return "trash"
        return "review"

    def propose_merge(
        self,
        log_column: str,
        *,
        co_peers: frozenset[str] | None = None,
    ) -> MergeCandidate:
        log_s = sanitize_text(log_column)
        gl = self._glossary_lookup(log_s)
        if gl is not None and gl in self.ddl_columns:
            vector_sim = 0.0
            if self.ddl_columns and len(self.ddl_columns) <= _MAX_PAIRWISE_DDL:
                pairs = self._pairwise_ddl_scores(log_s)
                if pairs:
                    vector_sim = pairs[0][1]
            cite = (
                f"Glossary semantic link: legacy token '{log_s}' maps to DDL column '{gl}' "
                f"(Hebrew/English business term alignment)."
            )
            return MergeCandidate(
                log_column=log_s,
                ddl_column=gl,
                merge_confidence=0.95,
                hitl=False,
                citation=cite,
                strategy="glossary",
                vector_similarity=vector_sim,
            )

        exact = self._exact_ddl_match(log_s)
        if exact:
            return MergeCandidate(
                log_column=log_s,
                ddl_column=exact,
                merge_confidence=0.98,
                hitl=False,
                citation="Exact normalized identifier match to a DDL column (no hash ambiguity).",
                strategy="exact_ddl",
                vector_similarity=1.0,
            )

        best_ddl = ""
        vector_sim = 0.0
        lex = 0.0
        blended = 0.0

        if self.ddl_columns and len(self.ddl_columns) <= _MAX_PAIRWISE_DDL:
            pairs = self._pairwise_ddl_scores(log_s)
            if pairs and co_peers and _is_ambiguous_token(log_s):
                boosted: list[tuple[str, float, float, float, float]] = []
                for d, v, lx, bl in pairs[:24]:
                    b = bl + _co_peer_boost(d, co_peers)
                    boosted.append((d, v, lx, bl, b))
                boosted.sort(key=lambda x: -x[4])
                best_ddl, vector_sim, lex, blended, _ = boosted[0]
            elif pairs:
                best_ddl, vector_sim, lex, blended = pairs[0]
        elif self.ddl_columns:
            hits = self._store.search_log_column(log_s, limit=8)
            if hits:
                best_ddl, vector_sim = hits[0]
            else:
                best_ddl = self.ddl_columns[0]
                vector_sim = 0.0
            lex = max((_lexical_similarity(log_s, d) for d in self.ddl_columns), default=0.0)
            blended = min(1.0, 0.55 * vector_sim + 0.45 * lex)

        llm_c: float | None = None
        llm_cite = ""
        if self._llm is not None:
            maybe = self._llm.reason(
                log_column=log_s,
                ddl_column=best_ddl,
                vector_similarity=vector_sim,
                glossary_hit=False,
                co_occurring_columns=tuple(sorted(co_peers)) if co_peers else None,
                ddl_column_list=tuple(self.ddl_columns),
            )
            if maybe is not None:
                llm_c, llm_cite = maybe

        if llm_c is not None:
            conf = max(blended, llm_c)
            cite = llm_cite or "LLM suggested alignment."
            strat = "llm"
        else:
            conf = blended
            cite = (
                f"Retrieval+lexical: best DDL candidate '{best_ddl}' "
                f"(vector_sim={vector_sim:.3f}, lexical={lex:.3f})."
            )
            strat = "vector_lexical"

        gen_penalty = 0.35 if is_generic_low_signal_name(log_s) else 1.0
        conf = max(0.0, min(1.0, conf * gen_penalty))

        if has_rtl_script(log_s) and gl is None and self._exact_ddl_match(log_s) is None:
            conf = min(conf, self.merge_floor * 0.8)

        hitl = conf < HITL_THRESHOLD
        return MergeCandidate(
            log_column=log_s,
            ddl_column=best_ddl,
            merge_confidence=conf,
            hitl=hitl,
            citation=cite,
            strategy=strat,
            vector_similarity=vector_sim,
        )

    def _canonical_bucket(self, raw_col: str, mc: MergeCandidate) -> str:
        gl = self._glossary_lookup(raw_col)
        if gl and gl in self.ddl_columns:
            return gl
        if mc.ddl_column and mc.ddl_column in self.ddl_columns:
            return mc.ddl_column
        return normalize_sql_identifier(raw_col)

    def merge_table_stats(self, stats: TableColumnStats, *, source_table: str = "") -> MergeResult:
        """
        Merge only high-confidence / glossary / exact mappings onto DDL keys.
        Low-confidence vector picks stay unmapped (review or trash buckets).
        """
        proposed: list[MergeCandidate] = []
        bucket: dict[str, list[tuple[str, ColumnStats, MergeCandidate]]] = defaultdict(list)
        review: list[UnmappedCandidate] = []
        trash: list[UnmappedCandidate] = []
        unmapped_stats = TableColumnStats(query_count=stats.query_count)

        for raw_col, cs in stats.columns.items():
            nk = normalize_sql_identifier(raw_col)
            peers = frozenset(stats.co_peers.get(nk, set())) if nk else frozenset()
            mc = self.propose_merge(raw_col, co_peers=peers if peers else None)
            proposed.append(mc)
            if self._merge_allowed_to_ddl(raw_col, mc):
                canon = self._canonical_bucket(raw_col, mc)
                bucket[canon].append((raw_col, cs, mc))
            else:
                unmapped_stats.columns[raw_col] = cs
                cat = self._unmapped_category(mc)
                uc = UnmappedCandidate(
                    legacy_name=raw_col,
                    suggested_ddl=mc.ddl_column,
                    merge_confidence=mc.merge_confidence,
                    category=cat,
                    citation=mc.citation,
                    strategy=mc.strategy,
                    stats=cs,
                    source_table=source_table,
                )
                if cat == "review":
                    review.append(uc)
                else:
                    trash.append(uc)

        merged_stats = TableColumnStats(query_count=stats.query_count)
        entities: list[MergedEntity] = []

        for canon, items in bucket.items():
            total = ColumnStats()
            sources: list[str] = []
            citations: list[str] = []
            strategies: list[str] = []
            merge_conf = max((mc.merge_confidence for _, _, mc in items), default=1.0)
            worst_hitl = any(mc.hitl for _, _, mc in items)
            for raw_col, cs, mc in items:
                sources.append(raw_col)
                strategies.append(mc.strategy)
                total.select += cs.select
                total.where += cs.where
                total.join_on += cs.join_on
                total.group_by += cs.group_by
                total.order_by += cs.order_by
                citations.append(f"{raw_col}→{canon}: {mc.citation}")
            merged_stats.columns[canon] = total
            entities.append(
                MergedEntity(
                    canonical_column=canon,
                    source_columns=sources,
                    combined_stats=total,
                    merge_confidence=merge_conf,
                    hitl=worst_hitl,
                    citations=citations,
                    strategies=strategies,
                    source_table=source_table,
                )
            )

        return MergeResult(
            merged_stats=merged_stats,
            unmapped_stats=unmapped_stats,
            confirmed_entities=entities,
            review_candidates=review,
            trash_candidates=trash,
            proposals=proposed,
        )


def load_ddl_columns(path: Path) -> list[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return [str(x) for x in data]
    if isinstance(data, dict) and "columns" in data:
        return [str(x) for x in data["columns"]]
    raise ValueError("DDL file must be a JSON list or {\"columns\": [...]}")
