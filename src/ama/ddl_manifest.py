"""
Map qualified table names (schema.table) to DDL column JSON paths for per-table alias merge.
"""

from __future__ import annotations

import json
from pathlib import Path

from ama.sanitize import normalize_sql_identifier


def normalize_manifest_table_key(key: str) -> str:
    """Stable key for manifest lookup (NFC, schema.table segments normalized)."""
    s = (key or "").strip()
    if not s:
        return ""
    parts = [p for p in s.split(".") if p]
    if len(parts) >= 2:
        return ".".join(normalize_sql_identifier(p) or p for p in parts[-2:])
    return normalize_sql_identifier(s) or s


def load_ddl_manifest(path: Path | None) -> dict[str, str]:
    """
    Load a flat JSON object: qualified table name -> path relative to data root.
    Keys starting with ``_`` are ignored (metadata).
    """
    if path is None or not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, str] = {}
    if not isinstance(data, dict):
        return out
    for k, v in data.items():
        if not isinstance(k, str) or not isinstance(v, str):
            continue
        if k.startswith("_"):
            continue
        nk = normalize_manifest_table_key(k)
        if nk:
            out[nk] = v.strip().replace("\\", "/")
    return out


def resolve_ddl_path_for_table(
    root: Path,
    manifest: dict[str, str],
    table_key: str,
    *,
    default_path: Path | None,
) -> Path | None:
    """
    Resolve the DDL JSON file for ``table_key``. Falls back to ``default_path`` when
    the table is missing from the manifest or the file is absent.
    """
    nk = normalize_manifest_table_key(table_key)
    rel = manifest.get(nk)
    if rel is None:
        for mk, mv in manifest.items():
            if mk.lower() == nk.lower():
                rel = mv
                break
    if rel:
        p = (root / rel).resolve()
        if p.is_file():
            return p
    if default_path is not None:
        if default_path.is_file():
            return default_path.resolve()
        p2 = (root / default_path).resolve()
        if p2.is_file():
            return p2
    return None
