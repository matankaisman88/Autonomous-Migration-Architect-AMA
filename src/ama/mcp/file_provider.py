"""
FileSchemaProvider — wraps existing DDL JSON manifest loading.
Used in tests, the Kfar Supply demo, and offline mode.
Produces identical results to the legacy _load_manifest_table_columns() function.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from ama.ddl_manifest import load_ddl_manifest
from ama.mcp.base import ColumnInfo, ExplainResult, SampleRow, SchemaProvider, TableSchema

logger = logging.getLogger(__name__)


class FileSchemaProvider(SchemaProvider):
    """
    Read-only provider backed by DDL JSON files on disk.
    No live DB connection. No PII masking needed (no real data).
    """

    def __init__(self, manifest_path: Path, data_root: Path):
        self._manifest_path = manifest_path
        self._data_root = data_root
        self._manifest: dict[str, str] = {}
        self._reload()

    def _reload(self) -> None:
        try:
            self._manifest = load_ddl_manifest(self._manifest_path)
        except Exception as exc:
            logger.warning("FileSchemaProvider: failed to load manifest: %s", exc)
            self._manifest = {}

    # ── SchemaProvider interface ───────────────────────────────────────────────

    def ping(self) -> bool:
        return self._manifest_path is None or self._manifest_path.exists()

    def list_tables(self, schema_filter: str | None = None) -> list[str]:
        if schema_filter:
            return [k for k in self._manifest if k.startswith(f"{schema_filter}.")]
        return list(self._manifest.keys())

    def get_table_schema(self, table_key: str) -> TableSchema | None:
        cols = self.get_columns(table_key)
        if not cols:
            return None
        parts = table_key.split(".", 1)
        schema = parts[0] if len(parts) == 2 else "public"
        table = parts[-1]
        return TableSchema(
            schema_name=schema,
            table_name=table,
            columns=[ColumnInfo(name=c, data_type="unknown") for c in cols],
        )

    def get_columns(self, table_key: str) -> list[str]:
        rel = self._manifest.get(table_key)
        if not rel:
            return []
        ddl_path = (self._data_root / rel).resolve()
        if not ddl_path.is_file():
            return []
        try:
            payload = json.loads(ddl_path.read_text(encoding="utf-8"))
        except Exception:
            return []
        cols = payload.get("columns") if isinstance(payload, dict) else None
        if isinstance(cols, list):
            return [str(c).strip() for c in cols if str(c).strip()]
        return []

    def get_sample_data(self, table_key: str, limit: int = 5) -> list[SampleRow]:
        # File provider has no live data — return empty (agent falls back to synthetic)
        return []

    def execute_explain(self, sql: str) -> ExplainResult:
        return ExplainResult(ok=True, plan="static_validation_only", dialect="static")

