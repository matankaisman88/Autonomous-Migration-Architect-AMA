"""
Detect SQL lineage edges that reference tables not listed in the DDL manifest.

Used by ingest (lineage JSON enrichment) and :class:`ama.planner.planner.AutonomousPlanner`
to flag migration rows without failing the pipeline.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from ama.ddl_manifest import normalize_manifest_table_key


def manifest_normalized_keys(manifest: dict[str, str]) -> set[str]:
    """Normalized manifest keys (metadata keys starting with ``_`` ignored)."""
    out: set[str] = set()
    for k in manifest.keys():
        if not isinstance(k, str) or k.startswith("_"):
            continue
        nk = normalize_manifest_table_key(k)
        if nk:
            out.add(nk)
    return out


def _listed_in_manifest(table_key: str, mk_norm: set[str]) -> bool:
    nk = normalize_manifest_table_key(table_key)
    if not nk:
        return False
    if nk in mk_norm:
        return True
    nkl = nk.lower()
    return any(m.lower() == nkl for m in mk_norm)


def neighbor_map_from_edges(edges: list[Any]) -> dict[str, set[str]]:
    """Undirected adjacency from lineage edges (directed pairs in report)."""
    adj: dict[str, set[str]] = defaultdict(set)
    for e in edges:
        if not isinstance(e, dict):
            continue
        a = str(e.get("from", "") or "").strip()
        b = str(e.get("to", "") or "").strip()
        if not a or not b or a == b:
            continue
        adj[a].add(b)
        adj[b].add(a)
    return {k: set(v) for k, v in adj.items()}


def enrich_lineage_payload(
    lineage: dict[str, Any],
    manifest: dict[str, str],
) -> dict[str, Any]:
    """
    Attach ``broken_table_keys`` and counts to the lineage dict for JSON export / UI.

    A table key is **broken** (manifest-unknown) if it does not appear in ``manifest``
    after :func:`normalize_manifest_table_key` matching.
    """
    out = dict(lineage) if lineage else {}
    edges = out.get("edges") or []
    mk = manifest_normalized_keys(manifest)
    endpoints: set[str] = set()
    for e in edges:
        if not isinstance(e, dict):
            continue
        a = str(e.get("from", "") or "").strip()
        b = str(e.get("to", "") or "").strip()
        if a:
            endpoints.add(a)
        if b:
            endpoints.add(b)
    broken = sorted(t for t in endpoints if t and not _listed_in_manifest(t, mk))
    out["broken_table_keys"] = broken
    out["broken_lineage_count"] = len(broken)
    return out


def compute_planner_breakage(
    report: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """
    For each discovery inventory ``full_name``, compute ``is_broken``, ``missing_parents``, ``reason``.
    Also return ``ghost_placeholders``: manifest-unknown endpoints that are **not** in inventory.

    Uses ``report['ddl_manifest_table_keys']`` (normalized keys from ingest). If absent, returns
    empty dict and no placeholders (backward compatible).
    """
    mk_raw = report.get("ddl_manifest_table_keys")
    if not isinstance(mk_raw, list) or not mk_raw:
        return {}, []

    mk_norm = {str(x).strip() for x in mk_raw if str(x).strip()}
    if not mk_norm:
        return {}, []

    lineage = report.get("lineage") if isinstance(report.get("lineage"), dict) else {}
    edges = lineage.get("edges") or []
    adj = neighbor_map_from_edges(edges)

    disc = report.get("discovery") or {}
    inv = disc.get("inventory") if isinstance(disc.get("inventory"), list) else []
    rows = [r for r in inv if isinstance(r, dict)]
    inventory_names = {str(r.get("full_name") or "").strip() for r in rows}
    inventory_names = {n for n in inventory_names if n}

    per_table: dict[str, dict[str, Any]] = {}
    all_endpoints: set[str] = set()
    for e in edges:
        if not isinstance(e, dict):
            continue
        a = str(e.get("from", "") or "").strip()
        b = str(e.get("to", "") or "").strip()
        if a:
            all_endpoints.add(a)
        if b:
            all_endpoints.add(b)

    for fn in sorted(inventory_names):
        missing_parents = sorted(
            n
            for n in adj.get(fn, ())
            if n and not _listed_in_manifest(n, mk_norm)
        )
        self_unlisted = not _listed_in_manifest(fn, mk_norm)
        is_broken = bool(missing_parents) or self_unlisted
        reason_parts: list[str] = []
        if missing_parents:
            show = ", ".join(missing_parents[:8])
            if len(missing_parents) > 8:
                show += ", …"
            reason_parts.append(f"Co-queries with manifest-unknown table(s): {show}")
        if self_unlisted:
            reason_parts.append("Table not listed in DDL manifest")
        reason = " · ".join(reason_parts) if reason_parts else ""
        per_table[fn] = {
            "is_broken": is_broken,
            "missing_parents": missing_parents,
            "reason": reason,
        }

    ghost_placeholders: list[str] = []
    for t in sorted(all_endpoints):
        if not t or t in inventory_names:
            continue
        if not _listed_in_manifest(t, mk_norm):
            ghost_placeholders.append(t)
    ghost_placeholders = sorted(set(ghost_placeholders))
    return per_table, ghost_placeholders
