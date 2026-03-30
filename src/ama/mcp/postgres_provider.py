"""
PostgresSchemaProvider — live schema introspection via psycopg2.

Requirements:
  pip install psycopg2-binary

Connection string format: postgresql://user:password@host:port/dbname
"""
from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from typing import Any, Generator

from ama.mcp.base import ColumnInfo, ExplainResult, SampleRow, SchemaProvider, TableSchema
from ama.mcp.pii import mask_rows

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 10


class PostgresSchemaProvider(SchemaProvider):
    """
    Live Postgres provider.

    Connection lifecycle:
      - Connections are opened per-operation using a context manager.
      - No persistent connection held between calls (stateless per request).
      - Hard 10-second timeout enforced at both connect and query level.
    """

    def __init__(self, connection_string: str, timeout_seconds: int = _TIMEOUT_SECONDS):
        self._conn_str = connection_string
        self._timeout = timeout_seconds
        self._db_version: str | None = None

    @contextmanager
    def _connect(self) -> Generator[Any, None, None]:
        """
        Context manager: open → yield cursor-bearing connection → close.
        Always closes connection even on exception.
        """
        try:
            import psycopg2
            import psycopg2.extras
        except ImportError:
            raise RuntimeError(
                "psycopg2-binary is required: pip install psycopg2-binary"
            )

        conn = None
        try:
            conn = psycopg2.connect(
                self._conn_str,
                connect_timeout=self._timeout,
                options=f"-c statement_timeout={self._timeout * 1000}",
            )
            conn.autocommit = True
            yield conn
        except psycopg2.OperationalError as exc:
            logger.error("Postgres connection failed: %s", exc)
            raise
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    # ── SchemaProvider interface ───────────────────────────────────────────────

    def ping(self) -> bool:
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT version()")
                    row = cur.fetchone()
                    if row:
                        self._db_version = str(row[0])
            return True
        except Exception as exc:
            logger.warning("Postgres ping failed: %s", exc)
            return False

    def get_db_version(self) -> str | None:
        """Return the cached Postgres version string (populated after ping())."""
        return self._db_version

    def list_tables(self, schema_filter: str | None = None) -> list[str]:
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    if schema_filter:
                        cur.execute(
                            """
                            SELECT table_schema, table_name
                            FROM information_schema.tables
                            WHERE table_type = 'BASE TABLE'
                              AND table_schema = %s
                            ORDER BY table_schema, table_name
                            """,
                            (schema_filter,),
                        )
                    else:
                        cur.execute(
                            """
                            SELECT table_schema, table_name
                            FROM information_schema.tables
                            WHERE table_type = 'BASE TABLE'
                              AND table_schema NOT IN ('pg_catalog', 'information_schema')
                            ORDER BY table_schema, table_name
                            """
                        )
                    return [f"{row[0]}.{row[1]}" for row in cur.fetchall()]
        except Exception as exc:
            logger.error("Postgres list_tables failed: %s", exc)
            return []

    def get_table_schema(self, table_key: str) -> TableSchema | None:
        parts = table_key.split(".", 1)
        if len(parts) != 2:
            return None
        schema_name, table_name = parts
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT
                            c.column_name,
                            c.data_type,
                            c.is_nullable,
                            CASE WHEN pk.column_name IS NOT NULL THEN TRUE ELSE FALSE END
                        FROM information_schema.columns c
                        LEFT JOIN (
                            SELECT ku.column_name
                            FROM information_schema.table_constraints tc
                            JOIN information_schema.key_column_usage ku
                                ON tc.constraint_name = ku.constraint_name
                               AND tc.table_schema = ku.table_schema
                               AND tc.table_name = ku.table_name
                            WHERE tc.constraint_type = 'PRIMARY KEY'
                              AND tc.table_schema = %s AND tc.table_name = %s
                        ) pk ON c.column_name = pk.column_name
                        WHERE c.table_schema = %s AND c.table_name = %s
                        ORDER BY c.ordinal_position
                        """,
                        (schema_name, table_name, schema_name, table_name),
                    )
                    rows = cur.fetchall()
                    if not rows:
                        return None

                    # Row count via pg_class — fast, no table scan
                    row_count: int | None = None
                    try:
                        cur.execute(
                            "SELECT reltuples::bigint FROM pg_class "
                            "WHERE relname = %s AND relnamespace = "
                            "(SELECT oid FROM pg_namespace WHERE nspname = %s)",
                            (table_name, schema_name),
                        )
                        rc_row = cur.fetchone()
                        row_count = int(rc_row[0]) if rc_row and rc_row[0] >= 0 else None
                    except Exception:
                        pass

                    return TableSchema(
                        schema_name=schema_name,
                        table_name=table_name,
                        row_count_estimate=row_count,
                        columns=[
                            ColumnInfo(
                                name=row[0],
                                data_type=row[1],
                                nullable=(row[2] == "YES"),
                                primary_key=bool(row[3]),
                            )
                            for row in rows
                        ],
                    )
        except Exception as exc:
            logger.error("Postgres get_table_schema(%s) failed: %s", table_key, exc)
            return None

    def get_columns(self, table_key: str) -> list[str]:
        ts = self.get_table_schema(table_key)
        return [c.name for c in ts.columns] if ts else []

    def get_sample_data(self, table_key: str, limit: int = 5) -> list[SampleRow]:
        """
        Fetch real rows — PII-masked before returning.
        Uses a read-only query with LIMIT; never modifies data.
        """
        parts = table_key.split(".", 1)
        if len(parts) != 2:
            return []
        schema_name, table_name = parts
        cap = max(1, min(int(limit), 100))  # hard cap at 100 rows
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f'SELECT * FROM "{schema_name}"."{table_name}" LIMIT %s',
                        (cap,),
                    )
                    cols = [desc[0] for desc in (cur.description or [])]
                    raw_rows = [
                        {cols[i]: row[i] for i in range(min(len(cols), len(row)))}
                        for row in cur.fetchall()
                    ]
                    masked = mask_rows(raw_rows)
                    return [SampleRow(data=r) for r in masked]
        except Exception as exc:
            logger.error("Postgres get_sample_data(%s) failed: %s", table_key, exc)
            return []

    def execute_explain(self, sql: str) -> ExplainResult:
        """
        EXPLAIN (FORMAT JSON) — safe read-only plan fetch.
        Returns structured JSON plan from Postgres optimizer.
        """
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(f"EXPLAIN (FORMAT JSON) {sql}")
                    rows = cur.fetchall()
                    plan_text = json.dumps(rows[0][0], ensure_ascii=False, indent=2) if rows else ""
                    return ExplainResult(ok=True, plan=plan_text, dialect="postgres")
        except Exception as exc:
            error_msg = str(exc)
            logger.warning("Postgres EXPLAIN failed: %s", error_msg)
            return ExplainResult(ok=False, plan="", error=error_msg, dialect="postgres")

