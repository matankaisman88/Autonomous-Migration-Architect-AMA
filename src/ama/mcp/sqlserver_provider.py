"""
SQLServerSchemaProvider — live schema introspection via pyodbc.

Connection string format is passed directly to `pyodbc.connect`.

This provider is read-only:
  - It never executes DDL/DML; EXPLAIN uses SHOWPLAN_XML which does not execute the query.
  - It attempts to set ApplicationIntent=ReadOnly when not already present.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Generator

from ama.mcp.base import ColumnInfo, ExplainResult, SampleRow, SchemaProvider, TableSchema
from ama.mcp.pii import mask_row

logger = logging.getLogger(__name__)


class SQLServerSchemaProvider(SchemaProvider):
    """
    Live SQL Server provider.

    Notes:
      - Uses INFORMATION_SCHEMA for discovery.
      - Uses `SET SHOWPLAN_XML ON` for EXPLAIN.
      - Sampling uses `SELECT TOP {limit} * ...` and masks PII on every returned row.
    """

    def __init__(self, connection_string: str, timeout_seconds: int = 10):
        self._conn_str = str(connection_string)
        self._timeout = int(timeout_seconds)

    def _augment_connection_string_readonly(self, conn_str: str) -> str:
        """
        Best-effort: add ApplicationIntent=ReadOnly if caller didn't specify it.
        """
        cs = conn_str.strip()
        if not cs:
            return cs
        lowered = cs.lower()
        if "applicationintent=" in lowered:
            return cs
        if not cs.endswith(";"):
            cs += ";"
        return cs + "ApplicationIntent=ReadOnly;"

    def _quote_ident(self, ident: str) -> str:
        """
        Quote an identifier for T-SQL using brackets: [name].
        Escapes any embedded closing bracket: ] -> ]]
        """
        safe = str(ident).replace("]", "]]")
        return f"[{safe}]"

    @contextmanager
    def _connect(self) -> Generator[Any, None, None]:
        """
        Context manager: open → yield connection → close always.
        """
        conn = None
        try:
            import pyodbc  # lazy import: required only when sqlserver mode is active

            conn_str = self._augment_connection_string_readonly(self._conn_str)
            conn = pyodbc.connect(conn_str, timeout=self._timeout)
            conn.autocommit = True
            yield conn
        except Exception as exc:
            logger.warning("SQLServer connection failed: %s", exc)
            raise
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def _set_query_timeout(self, cursor: Any) -> None:
        # pyodbc uses cursor.timeout for query execution timeout (seconds).
        try:
            cursor.timeout = self._timeout
        except Exception:
            pass

    # ── SchemaProvider interface ───────────────────────────────────────────────

    def ping(self) -> bool:
        try:
            with self._connect() as conn:
                cur = conn.cursor()
                self._set_query_timeout(cur)
                cur.execute("SELECT 1 AS ok")
                _ = cur.fetchone()
                return True
        except Exception as exc:
            logger.warning("SQLServer ping failed: %s", exc)
            return False

    def list_tables(self, schema_filter: str | None = None) -> list[str]:
        try:
            with self._connect() as conn:
                cur = conn.cursor()
                self._set_query_timeout(cur)
                if schema_filter:
                    cur.execute(
                        """
                        SELECT TABLE_SCHEMA, TABLE_NAME
                        FROM INFORMATION_SCHEMA.TABLES
                        WHERE TABLE_TYPE = 'BASE TABLE'
                          AND TABLE_SCHEMA = ?
                        ORDER BY TABLE_SCHEMA, TABLE_NAME
                        """,
                        (schema_filter,),
                    )
                else:
                    cur.execute(
                        """
                        SELECT TABLE_SCHEMA, TABLE_NAME
                        FROM INFORMATION_SCHEMA.TABLES
                        WHERE TABLE_TYPE = 'BASE TABLE'
                        ORDER BY TABLE_SCHEMA, TABLE_NAME
                        """
                    )
                rows = cur.fetchall()
                return [f"{str(r[0])}.{str(r[1])}" for r in rows]
        except Exception as exc:
            logger.error("SQLServer list_tables failed: %s", exc)
            return []

    def get_table_schema(self, table_key: str) -> TableSchema | None:
        """
        Returns TableSchema with ColumnInfo containing data_type + nullability + PK hint.
        """
        parts = str(table_key).split(".", 1)
        if len(parts) != 2:
            return None
        schema_name, table_name = parts[0], parts[1]
        schema_name = schema_name.strip()
        table_name = table_name.strip()
        if not schema_name or not table_name:
            return None

        try:
            with self._connect() as conn:
                cur = conn.cursor()
                self._set_query_timeout(cur)

                # Single query: columns + PK detection via LEFT JOIN on constraints.
                cur.execute(
                    """
                    SELECT
                        c.COLUMN_NAME,
                        c.DATA_TYPE,
                        c.IS_NULLABLE,
                        CASE WHEN pk.COLUMN_NAME IS NULL THEN 0 ELSE 1 END AS IS_PRIMARY_KEY
                    FROM INFORMATION_SCHEMA.COLUMNS c
                    LEFT JOIN (
                        SELECT
                            kcu.COLUMN_NAME
                        FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
                        JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
                          ON tc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME
                         AND tc.TABLE_SCHEMA = kcu.TABLE_SCHEMA
                         AND tc.TABLE_NAME = kcu.TABLE_NAME
                        WHERE tc.CONSTRAINT_TYPE = 'PRIMARY KEY'
                          AND tc.TABLE_SCHEMA = ?
                          AND tc.TABLE_NAME = ?
                    ) pk
                      ON c.COLUMN_NAME = pk.COLUMN_NAME
                    WHERE c.TABLE_SCHEMA = ?
                      AND c.TABLE_NAME = ?
                    ORDER BY c.ORDINAL_POSITION
                    """,
                    (schema_name, table_name, schema_name, table_name),
                )

                rows = cur.fetchall()
                if not rows:
                    return None

                cols: list[ColumnInfo] = []
                for r in rows:
                    col_name = str(r[0])
                    data_type = str(r[1])
                    is_nullable = str(r[2]).upper() == "YES"
                    is_pk = bool(int(r[3])) if r[3] is not None else False
                    cols.append(
                        ColumnInfo(
                            name=col_name,
                            data_type=data_type,
                            nullable=is_nullable,
                            primary_key=is_pk,
                        )
                    )

                return TableSchema(
                    schema_name=schema_name,
                    table_name=table_name,
                    columns=cols,
                    row_count_estimate=None,
                )
        except Exception as exc:
            logger.error("SQLServer get_table_schema(%s) failed: %s", table_key, exc)
            return None

    def get_columns(self, table_key: str) -> list[str]:
        ts = self.get_table_schema(table_key)
        return [c.name for c in ts.columns] if ts is not None else []

    def get_sample_data(self, table_key: str, limit: int = 5) -> list[SampleRow]:
        parts = str(table_key).split(".", 1)
        if len(parts) != 2:
            return []
        schema_name, table_name = parts[0], parts[1]

        cap = max(1, min(int(limit), 100))  # hard cap
        try:
            with self._connect() as conn:
                cur = conn.cursor()
                self._set_query_timeout(cur)

                # Read-only sampling: TOP N rows.
                sql = (
                    f"SELECT TOP {cap} * FROM {self._quote_ident(schema_name)}.{self._quote_ident(table_name)}"
                )
                cur.execute(sql)
                cols = [str(d[0]) for d in (cur.description or [])]
                raw_rows = [
                    {cols[i]: row[i] for i in range(min(len(cols), len(row)))}
                    for row in cur.fetchall()
                ]
                masked_rows: list[SampleRow] = []
                for row in raw_rows:
                    # Mandatory: mask every row before returning.
                    masked = mask_row(row)
                    masked_rows.append(SampleRow(data=masked))
                return masked_rows
        except Exception as exc:
            logger.error("SQLServer get_sample_data(%s) failed: %s", table_key, exc)
            return []

    def execute_explain(self, sql: str) -> ExplainResult:
        """
        Use SET SHOWPLAN_XML ON to capture optimizer/execution plan as XML text.
        """
        stmt = str(sql or "").strip()
        if not stmt:
            return ExplainResult(ok=False, plan="", error="Empty SQL.", dialect="sqlserver")

        try:
            with self._connect() as conn:
                cur = conn.cursor()
                self._set_query_timeout(cur)

                stmt_id = None
                try:
                    # SHOWPLAN_XML ON compiles and returns plan rather than executing.
                    cur.execute("SET SHOWPLAN_XML ON")
                    cur.execute(stmt)
                    rows = cur.fetchall()
                    xml_parts: list[str] = []
                    for r in rows:
                        # pyodbc returns a tuple; first column is the XML.
                        if r and r[0] is not None:
                            xml_parts.append(str(r[0]))
                    plan_text = "\n".join(xml_parts)
                    return ExplainResult(ok=True, plan=plan_text, dialect="sqlserver")
                finally:
                    try:
                        cur.execute("SET SHOWPLAN_XML OFF")
                    except Exception:
                        # Cleanup best-effort; don't mask the original result.
                        logger.warning("SQLServer failed to turn SHOWPLAN_XML OFF")
        except Exception as exc:
            logger.error("SQLServer EXPLAIN failed: %s", exc)
            return ExplainResult(ok=False, plan="", error=str(exc), dialect="sqlserver")

