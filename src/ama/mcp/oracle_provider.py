"""
OracleSchemaProvider — live schema introspection via python-oracledb (thin mode).

Requirements:
  pip install oracledb

Connection string format: user/password@host:port/service_name
  OR DSN: user/password@(DESCRIPTION=(ADDRESS=(PROTOCOL=TCP)(HOST=...)(PORT=...))(CONNECT_DATA=(SERVICE_NAME=...)))
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Generator

from ama.mcp.base import ColumnInfo, ExplainResult, SampleRow, SchemaProvider, TableSchema
from ama.mcp.pii import mask_rows

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 10


class OracleSchemaProvider(SchemaProvider):
    """
    Live Oracle provider using python-oracledb in thin mode (no Oracle Client needed).

    Connection lifecycle: context manager per operation — always closed after use.
    """

    def __init__(self, connection_string: str, timeout_seconds: int = _TIMEOUT_SECONDS):
        self._conn_str = connection_string
        self._timeout = timeout_seconds
        self._db_version: str | None = None

    def _parse_conn_str(self) -> dict[str, str]:
        """
        Parse 'user/password@host:port/service' into kwargs for oracledb.connect().
        Supports simple format only; DSN strings are passed through as-is.
        """
        cs = self._conn_str.strip()
        # Simple format: user/pass@host:port/service
        if "@" in cs and "/" in cs.split("@")[0]:
            user_pass, dsn = cs.split("@", 1)
            user, password = user_pass.split("/", 1)
            return {"user": user, "password": password, "dsn": dsn}
        # Fallback: treat entire string as DSN
        return {"dsn": cs}

    @contextmanager
    def _connect(self) -> Generator[Any, None, None]:
        """Context manager: open → yield connection → close always."""
        try:
            import oracledb
        except ImportError:
            raise RuntimeError(
                "python-oracledb is required: pip install oracledb"
            )

        conn = None
        try:
            kwargs = self._parse_conn_str()
            # Thin mode: no Oracle Client installation needed
            conn = oracledb.connect(**kwargs, tcp_connect_timeout=self._timeout)
            yield conn
        except oracledb.DatabaseError as exc:
            logger.error("Oracle connection failed: %s", exc)
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
                cur = conn.cursor()
                cur.execute("SELECT banner FROM v$version WHERE ROWNUM = 1")
                row = cur.fetchone()
                if row:
                    self._db_version = str(row[0])
            return True
        except Exception as exc:
            logger.warning("Oracle ping failed: %s", exc)
            return False

    def get_db_version(self) -> str | None:
        return self._db_version

    def list_tables(self, schema_filter: str | None = None) -> list[str]:
        """
        Query ALL_TABLES for accessible tables.
        schema_filter maps to OWNER column (Oracle schemas = owners).
        """
        try:
            with self._connect() as conn:
                cur = conn.cursor()
                if schema_filter:
                    cur.execute(
                        "SELECT OWNER, TABLE_NAME FROM ALL_TABLES "
                        "WHERE OWNER = :1 ORDER BY OWNER, TABLE_NAME",
                        [schema_filter.upper()],
                    )
                else:
                    cur.execute(
                        "SELECT OWNER, TABLE_NAME FROM ALL_TABLES "
                        "WHERE OWNER NOT IN ("
                        "  'SYS','SYSTEM','DBSNMP','OUTLN','MDSYS','ORDSYS',"
                        "  'EXFSYS','DMSYS','WMSYS','CTXSYS','ANONYMOUS',"
                        "  'XDB','ORDPLUGINS','ORDDATA','SI_INFORMTN_SCHEMA',"
                        "  'OLAPSYS','MDDATA','SPATIAL_WFS_ADMIN_USR',"
                        "  'SPATIAL_CSW_ADMIN_USR','SYSMAN','APEX_040000',"
                        "  'APEX_PUBLIC_USER','FLOWS_FILES','HR','OE','PM',"
                        "  'IX','BI','SCOTT','DEMO'"
                        ") ORDER BY OWNER, TABLE_NAME"
                    )
                return [f"{row[0]}.{row[1]}" for row in cur.fetchall()]
        except Exception as exc:
            logger.error("Oracle list_tables failed: %s", exc)
            return []

    def get_table_schema(self, table_key: str) -> TableSchema | None:
        parts = table_key.split(".", 1)
        if len(parts) != 2:
            return None
        owner, table_name = parts[0].upper(), parts[1].upper()
        try:
            with self._connect() as conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT
                        c.COLUMN_NAME,
                        c.DATA_TYPE,
                        c.NULLABLE,
                        CASE WHEN pk.COLUMN_NAME IS NOT NULL THEN 1 ELSE 0 END
                    FROM ALL_TAB_COLUMNS c
                    LEFT JOIN (
                        SELECT cc.COLUMN_NAME
                        FROM ALL_CONSTRAINTS con
                        JOIN ALL_CONS_COLUMNS cc
                            ON con.CONSTRAINT_NAME = cc.CONSTRAINT_NAME
                           AND con.OWNER = cc.OWNER
                        WHERE con.CONSTRAINT_TYPE = 'P'
                          AND con.OWNER = :owner AND con.TABLE_NAME = :tbl
                    ) pk ON c.COLUMN_NAME = pk.COLUMN_NAME
                    WHERE c.OWNER = :owner AND c.TABLE_NAME = :tbl
                    ORDER BY c.COLUMN_ID
                    """,
                    {"owner": owner, "tbl": table_name},
                )
                rows = cur.fetchall()
                if not rows:
                    return None

                # Row count from ALL_TABLES stats (no full scan)
                row_count: int | None = None
                try:
                    cur.execute(
                        "SELECT NUM_ROWS FROM ALL_TABLES WHERE OWNER = :1 AND TABLE_NAME = :2",
                        [owner, table_name],
                    )
                    rc_row = cur.fetchone()
                    row_count = int(rc_row[0]) if rc_row and rc_row[0] is not None else None
                except Exception:
                    pass

                return TableSchema(
                    schema_name=owner,
                    table_name=table_name,
                    row_count_estimate=row_count,
                    columns=[
                        ColumnInfo(
                            name=row[0],
                            data_type=row[1],
                            nullable=(row[2] == "Y"),
                            primary_key=bool(row[3]),
                        )
                        for row in rows
                    ],
                )
        except Exception as exc:
            logger.error("Oracle get_table_schema(%s) failed: %s", table_key, exc)
            return None

    def get_columns(self, table_key: str) -> list[str]:
        ts = self.get_table_schema(table_key)
        return [c.name for c in ts.columns] if ts else []

    def get_sample_data(self, table_key: str, limit: int = 5) -> list[SampleRow]:
        """
        ROWNUM-limited SELECT — PII-masked before returning.
        Oracle syntax: SELECT * FROM owner.table WHERE ROWNUM <= N
        """
        parts = table_key.split(".", 1)
        if len(parts) != 2:
            return []
        owner, table_name = parts[0].upper(), parts[1].upper()
        cap = max(1, min(int(limit), 100))
        try:
            with self._connect() as conn:
                cur = conn.cursor()
                cur.execute(
                    f'SELECT * FROM "{owner}"."{table_name}" WHERE ROWNUM <= :1',
                    [cap],
                )
                cols = [desc[0] for desc in (cur.description or [])]
                raw_rows = [
                    {cols[i]: row[i] for i in range(min(len(cols), len(row)))}
                    for row in cur.fetchall()
                ]
                masked = mask_rows(raw_rows)
                return [SampleRow(data=r) for r in masked]
        except Exception as exc:
            logger.error("Oracle get_sample_data(%s) failed: %s", table_key, exc)
            return []

    def execute_explain(self, sql: str) -> ExplainResult:
        """
        Oracle EXPLAIN PLAN FOR + DBMS_XPLAN.DISPLAY.
        Creates a PLAN_TABLE entry then reads it. Read-only with respect to user data.
        """
        try:
            with self._connect() as conn:
                cur = conn.cursor()
                # Write plan into PLAN_TABLE (Oracle internal — not user data)
                cur.execute(f"EXPLAIN PLAN FOR {sql}")
                # Retrieve the formatted plan
                cur.execute(
                    "SELECT PLAN_TABLE_OUTPUT "
                    "FROM TABLE(DBMS_XPLAN.DISPLAY('PLAN_TABLE', NULL, 'BASIC +ROWS +COST'))"
                )
                lines = [str(row[0]) for row in cur.fetchall()]
                plan_text = "\n".join(lines)
                return ExplainResult(ok=True, plan=plan_text, dialect="oracle")
        except Exception as exc:
            error_msg = str(exc)
            logger.warning("Oracle EXPLAIN failed: %s", error_msg)
            return ExplainResult(ok=False, plan="", error=error_msg, dialect="oracle")

