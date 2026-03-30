"""
Factory: returns the correct SchemaProvider based on AMA_SCHEMA_MODE.

Mode resolution order:
  1. AMA_SCHEMA_MODE environment variable
  2. `mode` parameter passed to get_schema_provider()
  3. Default: "file"

All provider imports are lazy (inside branches) so missing optional
dependencies (psycopg2, oracledb, cryptography) don't crash the import.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from ama.mcp.base import SchemaProvider

logger = logging.getLogger(__name__)


def get_schema_provider(
    *,
    mode: str = "file",
    connection_string: str | None = None,
    manifest_path: Path | None = None,
    data_root: Path | None = None,
    timeout_seconds: int = 10,
    encrypted: bool = False,
) -> SchemaProvider:
    """
    Factory function called by FastAPI routes and agent tools.

    Parameters
    ----------
    mode : "file" | "postgres" | "oracle"
    connection_string : required when mode != "file"
    manifest_path : DDL manifest JSON path (file mode only)
    data_root : base dir for resolving DDL paths (file mode only)
    timeout_seconds : hard DB timeout (live modes)
    encrypted : if True, decrypt connection_string using AMA_ENCRYPTION_KEY
    """
    resolved_mode = os.environ.get("AMA_SCHEMA_MODE", mode).lower().strip()
    # Prefer the explicit `connection_string` argument (e.g. passed by API request),
    # but fall back to `AMA_DB_CONNECTION_STRING` env var when the argument is
    # missing/empty. This avoids accidentally ignoring request-provided credentials.
    env_conn = os.environ.get("AMA_DB_CONNECTION_STRING", "").strip()
    arg_conn = (connection_string or "").strip()
    raw_conn = arg_conn if arg_conn else env_conn

    if encrypted and raw_conn:
        from ama.mcp.encryption import decrypt
        try:
            raw_conn = decrypt(raw_conn)
        except Exception as exc:
            raise ValueError(f"Failed to decrypt connection string: {exc}") from exc

    ts = int(os.environ.get("AMA_DB_TIMEOUT", str(timeout_seconds)))

    if resolved_mode == "postgres":
        if not raw_conn:
            raise ValueError(
                "AMA_SCHEMA_MODE=postgres requires AMA_DB_CONNECTION_STRING."
            )
        from ama.mcp.postgres_provider import PostgresSchemaProvider
        return PostgresSchemaProvider(connection_string=raw_conn, timeout_seconds=ts)

    elif resolved_mode == "oracle":
        if not raw_conn:
            raise ValueError(
                "AMA_SCHEMA_MODE=oracle requires AMA_DB_CONNECTION_STRING."
            )
        from ama.mcp.oracle_provider import OracleSchemaProvider
        return OracleSchemaProvider(connection_string=raw_conn, timeout_seconds=ts)

    elif resolved_mode == "sqlserver":
        if not raw_conn:
            raise ValueError(
                "AMA_SCHEMA_MODE=sqlserver requires AMA_DB_CONNECTION_STRING."
            )
        from ama.mcp.sqlserver_provider import SQLServerSchemaProvider
        return SQLServerSchemaProvider(connection_string=raw_conn, timeout_seconds=ts)

    else:  # "file" (default)
        from ama.mcp.file_provider import FileSchemaProvider
        mp = manifest_path or Path(os.environ.get("AMA_MANIFEST_PATH", "ddl_manifest.json"))
        dr = data_root or mp.parent
        return FileSchemaProvider(manifest_path=mp, data_root=dr)

