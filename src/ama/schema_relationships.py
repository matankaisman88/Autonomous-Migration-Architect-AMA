"""
PK/FK schema relationships from DDL manifest artifacts (explicit constraints or naming inference).
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ama.ddl_manifest import load_ddl_manifest, normalize_manifest_table_key, resolve_ddl_path_for_table
from ama.lineage import _adjacency_from_lineage_block


@dataclass(frozen=True)
class ForeignKeyEdge:
    """Directed FK: ``from_table`` references ``to_table`` via ``column`` → ``references_column``."""

    from_table: str
    to_table: str
    column: str
    references_column: str
    source: str  # "declared" | "inferred"


def _infer_primary_keys(table_key: str, columns: list[str], declared: list[str] | None) -> list[str]:
    if declared:
        return [c for c in declared if c in columns]
    if not columns:
        return []
    id_cols = [c for c in columns if c.lower().endswith("_id")]
    if len(id_cols) == 1:
        return id_cols
    table = table_key.rsplit(".", 1)[-1].lower()
    for c in columns:
        cl = c.lower()
        if cl == f"{table}_id":
            return [c]
        if table.endswith("s") and len(table) > 1 and cl == f"{table[:-1]}_id":
            return [c]
    if id_cols:
        return [id_cols[0]]
    return [columns[0]]


def load_ddl_metadata(path: Path) -> dict[str, Any]:
    """Parse DDL JSON: columns, optional primary_keys / foreign_keys."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        cols = [str(x) for x in data]
        return {"columns": cols, "primary_keys": [], "foreign_keys": []}
    if not isinstance(data, dict):
        raise ValueError("DDL file must be a JSON list or object")
    cols = [str(x) for x in (data.get("columns") or [])]
    pks_raw = data.get("primary_keys") or []
    pks = [str(x) for x in pks_raw] if isinstance(pks_raw, list) else []
    fks: list[dict[str, str]] = []
    for item in data.get("foreign_keys") or []:
        if not isinstance(item, dict):
            continue
        col = str(item.get("column") or "").strip()
        ref_table = str(item.get("references_table") or item.get("ref_table") or "").strip()
        ref_col = str(item.get("references_column") or item.get("ref_column") or col).strip()
        ref_full = str(item.get("references") or "").strip()
        if not col:
            continue
        if not ref_table and ref_full.count(".") >= 2:
            parts = ref_full.split(".")
            ref_table = ".".join(parts[:2])
            ref_col = parts[2] if len(parts) > 2 else col
        if ref_table:
            fks.append({"column": col, "references_table": ref_table, "references_column": ref_col})
    return {"columns": cols, "primary_keys": pks, "foreign_keys": fks}


def _parse_fk_ref(ref: str) -> tuple[str, str] | None:
    parts = [p for p in str(ref or "").strip().split(".") if p]
    if len(parts) < 3:
        return None
    return ".".join(parts[:2]), parts[2]


def build_foreign_key_edges(
    manifest: dict[str, str],
    data_root: Path,
    *,
    default_ddl_path: Path | None = None,
) -> list[ForeignKeyEdge]:
    """
    Build directed FK edges for all tables in ``manifest``.
    Uses declared ``foreign_keys`` in DDL JSON when present; otherwise naming inference.
    """
    root = data_root.resolve()
    catalog: dict[str, dict[str, Any]] = {}
    for raw_key, rel in manifest.items():
        if str(raw_key).startswith("_"):
            continue
        nk = normalize_manifest_table_key(str(raw_key))
        if not nk:
            continue
        ddl_path = resolve_ddl_path_for_table(root, manifest, nk, default_path=default_ddl_path)
        if ddl_path is None or not ddl_path.is_file():
            continue
        meta = load_ddl_metadata(ddl_path)
        meta["primary_keys"] = _infer_primary_keys(
            nk, meta["columns"], meta.get("primary_keys") or None
        )
        catalog[nk] = meta

    pk_index: dict[str, list[str]] = {}
    for tk, meta in catalog.items():
        for pk in meta["primary_keys"]:
            pk_index.setdefault(pk.lower(), []).append(tk)

    edges: list[ForeignKeyEdge] = []
    seen: set[tuple[str, str, str]] = set()

    def _add(from_t: str, to_t: str, col: str, ref_col: str, source: str) -> None:
        if from_t == to_t:
            return
        key = (from_t, to_t, col.lower())
        if key in seen:
            return
        seen.add(key)
        edges.append(
            ForeignKeyEdge(
                from_table=from_t,
                to_table=to_t,
                column=col,
                references_column=ref_col,
                source=source,
            )
        )

    for from_t, meta in catalog.items():
        declared_cols = {str(fk["column"]).lower() for fk in meta["foreign_keys"]}
        pk_set = {p.lower() for p in meta["primary_keys"]}
        for fk in meta["foreign_keys"]:
            col = str(fk["column"])
            to_t = normalize_manifest_table_key(str(fk["references_table"]))
            ref_col = str(fk.get("references_column") or col)
            if to_t in catalog:
                _add(from_t, to_t, col, ref_col, "declared")
        for col in meta["columns"]:
            cl = col.lower()
            if cl in pk_set or cl in declared_cols:
                continue
            if not cl.endswith("_id"):
                continue
            for parent in pk_index.get(cl, []):
                if parent != from_t:
                    _add(from_t, parent, col, col, "inferred")
                    break
    return edges


def resolve_report_data_root(report: dict[str, Any], report_path: Path | None = None) -> Path | None:
    """Best-effort data root (folder containing manifest + ddl/)."""
    if report_path is not None:
        return report_path.resolve().parent
    am = report.get("alias_merge") if isinstance(report.get("alias_merge"), dict) else {}
    manifest_raw = am.get("ddl_manifest")
    if isinstance(manifest_raw, str) and manifest_raw.strip():
        return Path(manifest_raw).resolve().parent
    logs = report.get("sql_log_files") or []
    if isinstance(logs, list) and logs and isinstance(logs[0], str):
        p = Path(logs[0])
        if "sql_logs" in p.parts:
            idx = p.parts.index("sql_logs")
            return Path(*p.parts[:idx])
    return None


_PK_FK_CACHE_LOCK = threading.Lock()
_PK_FK_EDGE_CACHE: dict[tuple[str, int], list[ForeignKeyEdge]] = {}


def clear_pk_fk_cache(*, report_id: str | None = None) -> None:
    with _PK_FK_CACHE_LOCK:
        if report_id is None:
            _PK_FK_EDGE_CACHE.clear()
            return
        dead = [k for k in _PK_FK_EDGE_CACHE if k[0] == report_id]
        for k in dead:
            del _PK_FK_EDGE_CACHE[k]


def foreign_key_edges_for_report(
    report: dict[str, Any],
    *,
    report_id: str | None = None,
    report_path: Path | None = None,
) -> list[ForeignKeyEdge]:
    data_root = resolve_report_data_root(report, report_path)
    if data_root is None or not data_root.is_dir():
        return []
    am = report.get("alias_merge") if isinstance(report.get("alias_merge"), dict) else {}
    manifest_path_raw = am.get("ddl_manifest")
    manifest_path = Path(manifest_path_raw) if isinstance(manifest_path_raw, str) and manifest_path_raw.strip() else data_root / "manifest.json"
    if not manifest_path.is_file():
        manifest_path = data_root / "manifest.json"
    if not manifest_path.is_file():
        return []
    cache_key = (report_id or "", id(report)) if report_id else None
    if cache_key:
        with _PK_FK_CACHE_LOCK:
            hit = _PK_FK_EDGE_CACHE.get(cache_key)
            if hit is not None:
                return hit
    manifest = load_ddl_manifest(manifest_path)
    ddl_src = am.get("ddl_source")
    default_ddl = Path(ddl_src) if isinstance(ddl_src, str) and ddl_src.strip() else None
    edges = build_foreign_key_edges(manifest, data_root, default_ddl_path=default_ddl)
    if cache_key:
        with _PK_FK_CACHE_LOCK:
            _PK_FK_EDGE_CACHE[cache_key] = edges
    return edges


def coquery_pair_weight(
    lineage_block: dict[str, Any] | None,
    table_a: str,
    table_b: str,
) -> int:
    """Undirected co-query count for two tables (0 if none)."""
    if not table_a or not table_b or table_a == table_b:
        return 0
    adj = _adjacency_from_lineage_block(lineage_block)
    return int(adj.get(table_a, {}).get(table_b, 0))


def query_counts_from_report(report: dict[str, Any]) -> dict[str, int]:
    """Map normalized table key → SQL log query_count from discovery inventory."""
    out: dict[str, int] = {}
    inv = (report.get("discovery") or {}).get("inventory") or []
    if not isinstance(inv, list):
        return out
    for row in inv:
        if not isinstance(row, dict):
            continue
        key = normalize_manifest_table_key(str(row.get("full_name") or ""))
        if not key:
            continue
        try:
            out[key] = int(row.get("query_count") or 0)
        except (TypeError, ValueError):
            out[key] = 0
    return out


def _format_fk_edge_label(column: str, coquery_count: int | None) -> str:
    if coquery_count is None:
        return f"FK {column}"
    if coquery_count > 0:
        n = coquery_count
        q = "query" if n == 1 else "queries"
        return f"FK {column} · {n} shared {q}"
    return f"FK {column} · no shared queries in logs"


def _format_coquery_edge_label(coquery_count: int) -> str:
    n = max(0, int(coquery_count))
    q = "query" if n == 1 else "queries"
    return f"{n} shared {q}"


def pk_fk_subgraph_payload(
    edges: list[ForeignKeyEdge],
    center_table_key: str,
    broken_keys: set[str] | frozenset[str] | None,
    *,
    lineage_block: dict[str, Any] | None = None,
    query_counts: dict[str, int] | None = None,
) -> dict[str, Any]:
    """
    1-hop PK/FK subgraph around ``center_table_key`` for React Flow.
    Directed edges: child (FK holder) → parent (PK target).
    """
    from ama.lineage import _radial_positions, _resolve_center_key

    center_raw = (center_table_key or "").strip()
    broken: set[str] = set(broken_keys or ())
    qcounts = query_counts or {}
    co_adj = _adjacency_from_lineage_block(lineage_block)
    if not center_raw:
        return {"nodes": [], "edges": [], "empty_reason": "missing_center_table"}

    # adjacency for undirected 1-hop neighbor discovery
    adj: dict[str, set[str]] = {}
    edge_by_pair: dict[tuple[str, str], ForeignKeyEdge] = {}
    for e in edges:
        adj.setdefault(e.from_table, set()).add(e.to_table)
        adj.setdefault(e.to_table, set()).add(e.from_table)
        edge_by_pair[(e.from_table, e.to_table)] = e

    all_tables = set(adj.keys()) | set(co_adj.keys())
    center = _resolve_center_key(center_raw, {t: co_adj.get(t, {}) for t in all_tables})
    if center not in all_tables and edges:
        center = center_raw

    neighbors: set[str] = set(adj.get(center, set()))
    for nb, _w in co_adj.get(center, {}).items():
        neighbors.add(nb)
    neighbors.discard(center)
    node_ids = [center] + sorted(n for n in neighbors if n != center)
    node_ids = list(dict.fromkeys(node_ids))
    positions = _radial_positions(center, node_ids)

    def _query_count_for(table_key: str) -> int | None:
        nk = normalize_manifest_table_key(table_key)
        if nk in qcounts:
            return qcounts[nk]
        lower = table_key.lower()
        for k, v in qcounts.items():
            if k.lower() == lower:
                return v
        return None

    broken_norm = {normalize_manifest_table_key(b) for b in broken}
    nodes: list[dict[str, Any]] = []
    for nid in node_ids:
        nk = normalize_manifest_table_key(nid)
        is_br = nk in broken_norm or nid in broken
        if nid == center:
            role = "broken" if is_br else "center"
        else:
            role = "broken" if is_br else "neighbor"
        x, y = positions.get(nid, (0.0, 0.0))
        qc = _query_count_for(nid)
        nodes.append(
            {
                "id": nid,
                "type": "lineageTable",
                "position": {"x": x, "y": y},
                "data": {
                    "label": nid,
                    "role": role,
                    "query_count": qc,
                },
            }
        )

    node_set = set(node_ids)
    fk_pairs: set[tuple[str, str]] = set()
    edges_out: list[dict[str, Any]] = []
    for e in edges:
        if e.from_table not in node_set or e.to_table not in node_set:
            continue
        if e.from_table not in neighbors and e.to_table not in neighbors and e.from_table != center and e.to_table != center:
            continue
        fk_pairs.add((e.from_table, e.to_table))
        co_ct = coquery_pair_weight(lineage_block, e.from_table, e.to_table)
        eid = f"fk|{e.from_table}|{e.column}|{e.to_table}"
        edges_out.append(
            {
                "id": eid,
                "source": e.from_table,
                "target": e.to_table,
                "data": {
                    "kind": "pk_fk",
                    "column": e.column,
                    "references_column": e.references_column,
                    "source": e.source,
                    "coquery_count": co_ct,
                },
                "label": _format_fk_edge_label(e.column, co_ct),
            }
        )

    seen_co_pairs: set[tuple[str, str]] = set()
    for nb in sorted(neighbors):
        if nb not in node_set:
            continue
        pair = (center, nb) if center < nb else (nb, center)
        if pair in seen_co_pairs:
            continue
        seen_co_pairs.add(pair)
        if (center, nb) in fk_pairs or (nb, center) in fk_pairs:
            continue
        co_ct = coquery_pair_weight(lineage_block, center, nb)
        if co_ct <= 0:
            continue
        ek = pair
        eid = f"coquery|{ek[0]}|{ek[1]}"
        edges_out.append(
            {
                "id": eid,
                "source": ek[0],
                "target": ek[1],
                "data": {"kind": "coquery", "coquery_count": co_ct, "weight": co_ct},
                "label": _format_coquery_edge_label(co_ct),
            }
        )

    empty_reason: str | None = None
    if not edges_out and not neighbors:
        empty_reason = "no_pk_fk_for_table" if edges or co_adj.get(center) else "no_pk_fk_edges"

    return {
        "nodes": nodes,
        "edges": edges_out,
        "empty_reason": empty_reason,
        "center_table_key": center,
        "lineage_mode": "pk_fk",
        "legend": (
            "Solid arrows: foreign-key direction (child → parent). "
            "Dashed links: tables joined in SQL logs only (no FK in DDL). "
            "Numbers on edges count queries where both tables appear in the same statement. "
            "Query counts on nodes count statements referencing that table."
        ),
    }
