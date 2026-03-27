"""
Map qualified table names (schema.table) to DDL column JSON paths for per-table alias merge.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ama.parsing.sqlglot_extract import extract_ddl_table_details
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


@dataclass(frozen=True)
class TableMetadata:
    """
    Source-table metadata carried from DDL manifests.

    The fields are intentionally extensible for future source dialects.
    """

    table_key: str
    ddl_path: str
    source_dialect: str | None = None
    owner: str | None = None
    tablespace: str | None = None
    database: str | None = None
    schema: str | None = None
    extras: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _entry_to_metadata(table_key: str, raw: Any) -> TableMetadata | None:
    nk = normalize_manifest_table_key(table_key)
    if not nk:
        return None
    if isinstance(raw, str):
        rel = raw.strip().replace("\\", "/")
        if not rel:
            return None
        return TableMetadata(table_key=nk, ddl_path=rel)
    if isinstance(raw, dict):
        p = raw.get("path") or raw.get("ddl_path")
        if not isinstance(p, str) or not p.strip():
            return None
        rel = p.strip().replace("\\", "/")
        src = raw.get("source_dialect")
        owner = raw.get("owner")
        ts = raw.get("tablespace")
        db = raw.get("database")
        schema = raw.get("schema")
        extras = {
            k: v
            for k, v in raw.items()
            if k not in {"path", "ddl_path", "source_dialect", "owner", "tablespace", "database", "schema"}
        }
        return TableMetadata(
            table_key=nk,
            ddl_path=rel,
            source_dialect=str(src).strip().lower() if isinstance(src, str) and src.strip() else None,
            owner=str(owner).strip() if isinstance(owner, str) and owner.strip() else None,
            tablespace=str(ts).strip() if isinstance(ts, str) and ts.strip() else None,
            database=str(db).strip() if isinstance(db, str) and db.strip() else None,
            schema=str(schema).strip() if isinstance(schema, str) and schema.strip() else None,
            extras=extras or None,
        )
    return None


def load_ddl_manifest_entries(path: Path | None) -> dict[str, TableMetadata]:
    """
    Load manifest entries as metadata records.

    Supports both flat values and rich objects:
    - ``"sales.orders": "sample_data/ddl/orders.json"``
    - ``"sales.orders": {"path": "...", "source_dialect": "oracle", "owner": "APP", "tablespace": "TS1"}``
    """
    if path is None or not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, TableMetadata] = {}
    if not isinstance(data, dict):
        return out
    for k, v in data.items():
        if not isinstance(k, str) or k.startswith("_"):
            continue
        md = _entry_to_metadata(k, v)
        if md is not None:
            out[md.table_key] = md
    return out


def load_ddl_manifest(path: Path | None) -> dict[str, str]:
    """
    Load a flat JSON object: qualified table name -> path relative to data root.
    Keys starting with ``_`` are ignored (metadata).
    """
    return {k: v.ddl_path for k, v in load_ddl_manifest_entries(path).items()}


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


def resolve_table_metadata_for_key(
    manifest_entries: dict[str, TableMetadata],
    table_key: str,
) -> TableMetadata | None:
    """
    Resolve table metadata by normalized key with case-insensitive fallback.
    """
    nk = normalize_manifest_table_key(table_key)
    md = manifest_entries.get(nk)
    if md is not None:
        return md
    for mk, mv in manifest_entries.items():
        if mk.lower() == nk.lower():
            return mv
    return None


def extract_manifest_entries_from_ddl_sql(
    ddl_sql: str,
    *,
    source_dialect: str | None = None,
    ddl_path: str = "",
) -> dict[str, TableMetadata]:
    """
    Parse CREATE TABLE statements into manifest metadata entries.
    """
    out: dict[str, TableMetadata] = {}
    for row in extract_ddl_table_details(ddl_sql, dialect=source_dialect):
        if not row.table_key:
            continue
        out[row.table_key] = TableMetadata(
            table_key=row.table_key,
            ddl_path=ddl_path,
            source_dialect=row.source_dialect or (source_dialect.lower() if source_dialect else None),
            owner=row.owner,
            tablespace=row.tablespace,
            database=row.database,
            schema=row.schema,
        )
    return out
