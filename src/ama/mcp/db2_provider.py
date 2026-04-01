"""
DB2 LUW SchemaProvider via ibm_db.

Metadata queries use WITH UR (uncommitted read) to avoid locking production tables.

Connection string (example)::

    DATABASE=mydb;HOSTNAME=localhost;PORT=50000;PROTOCOL=TCPIP;UID=user;PWD=secret;

Install: pip install ibm_db (optional extra ``db2``).
"""
from __future__ import annotations

import logging
import re
from contextlib import contextmanager
from typing import Any, Generator

from ama.mcp.base import ColumnInfo, ExplainResult, SampleRow, SchemaProvider, TableSchema
from ama.mcp.pii import mask_rows

logger = logging.getLogger(__name__)


def _import_ibm_db() -> Any:
    try:
        import ibm_db  # type: ignore[import-untyped]
    except ImportError as exc:
        raise RuntimeError("ibm_db is required for DB2 mode: pip install ibm_db") from exc
    return ibm_db


class DB2SchemaProvider(SchemaProvider):
    """Live Db2 LUW introspection; lazy-loads ``ibm_db``."""

    def __init__(self, connection_string: str, timeout_seconds: int = 10) -> None:
        self._conn_str = str(connection_string).strip()
        self._timeout = int(timeout_seconds)

    def _quote_ident(self, ident: str) -> str:
        s = str(ident).strip().replace('"', '""')
        return f'"{s}"'

    @contextmanager
    def _connect(self) -> Generator[Any, None, None]:
        ibm_db = _import_ibm_db()
        conn: Any = None
        try:
            conn = ibm_db.connect(self._conn_str, "", "")
            if conn is None:
                cem = getattr(ibm_db, "conn_errormsg", None)
                msg = cem() if callable(cem) else ""
                raise RuntimeError(msg or "ibm_db.connect returned None")
            yield conn
        except Exception as exc:
            logger.warning("DB2 connection failed: %s", exc)
            raise
        finally:
            if conn is not None:
                try:
                    ibm_db.close(conn)
                except Exception:
                    pass

    def _exec_fetchall(self, conn: Any, sql: str, params: tuple[Any, ...] | None = None) -> list[tuple[Any, ...]]:
        ibm_db = _import_ibm_db()
        stmt = None
        try:
            stmt = ibm_db.prepare(conn, sql)
            if stmt is None:
                raise RuntimeError(ibm_db.stmt_errormsg() or "prepare failed")
            if params:
                for i, val in enumerate(params, start=1):
                    ibm_db.bind_param(stmt, i, val)
            if ibm_db.execute(stmt) is None and ibm_db.stmt_error() != "":
                raise RuntimeError(ibm_db.stmt_errormsg() or "execute failed")
            rows: list[tuple[Any, ...]] = []
            row = ibm_db.fetch_tuple(stmt)
            while row:
                rows.append(tuple(row))
                row = ibm_db.fetch_tuple(stmt)
            return rows
        finally:
            if stmt is not None:
                try:
                    ibm_db.free_stmt(stmt)
                except Exception:
                    pass

    def ping(self) -> bool:
        try:
            with self._connect() as conn:
                self._exec_fetchall(conn, "SELECT 1 FROM SYSIBM.SYSDUMMY1 WITH UR")
            return True
        except Exception as exc:
            logger.warning("DB2 ping failed: %s", exc)
            return False

    def list_tables(self, schema_filter: str | None = None) -> list[str]:
        try:
            with self._connect() as conn:
                if schema_filter:
                    sf = schema_filter.strip().upper()
                    sql = (
                        "SELECT TABSCHEMA, TABNAME FROM SYSCAT.TABLES "
                        "WHERE TYPE = 'T' AND TABSCHEMA = ? WITH UR "
                        "ORDER BY TABSCHEMA, TABNAME"
                    )
                    rows = self._exec_fetchall(conn, sql, (sf,))
                else:
                    sql = (
                        "SELECT TABSCHEMA, TABNAME FROM SYSCAT.TABLES "
                        "WHERE TYPE = 'T' AND TABSCHEMA NOT LIKE 'SYS%' WITH UR "
                        "ORDER BY TABSCHEMA, TABNAME"
                    )
                    rows = self._exec_fetchall(conn, sql)
                return [f"{str(r[0]).strip()}.{str(r[1]).strip()}" for r in rows]
        except Exception as exc:
            logger.error("DB2 list_tables failed: %s", exc)
            return []

    def get_table_schema(self, table_key: str) -> TableSchema | None:
        parts = str(table_key).split(".", 1)
        if len(parts) != 2:
            return None
        schema_u, table_u = parts[0].strip().upper(), parts[1].strip().upper()
        try:
            with self._connect() as conn:
                col_sql = (
                    "SELECT c.COLNAME, c.TYPENAME, c.NULLS, c.KEYSEQ "
                    "FROM SYSCAT.COLUMNS c "
                    "WHERE c.TABSCHEMA = ? AND c.TABNAME = ? "
                    "ORDER BY c.COLNO WITH UR"
                )
                rows = self._exec_fetchall(conn, col_sql, (schema_u, table_u))
                if not rows:
                    return None
                cols: list[ColumnInfo] = []
                for r in rows:
                    name = str(r[0]).strip()
                    dtype = str(r[1]).strip()
                    nullable = str(r[2]).strip().upper() == "Y"
                    pk = r[3] is not None and str(r[3]).strip() != ""
                    cols.append(
                        ColumnInfo(
                            name=name,
                            data_type=dtype,
                            nullable=nullable,
                            primary_key=pk,
                        )
                    )
                return TableSchema(
                    schema_name=schema_u,
                    table_name=table_u,
                    columns=cols,
                    row_count_estimate=None,
                )
        except Exception as exc:
            logger.error("DB2 get_table_schema(%s) failed: %s", table_key, exc)
            return None

    def get_columns(self, table_key: str) -> list[str]:
        ts = self.get_table_schema(table_key)
        return [c.name for c in ts.columns] if ts else []

    def get_sample_data(self, table_key: str, limit: int = 5) -> list[SampleRow]:
        parts = str(table_key).split(".", 1)
        if len(parts) != 2:
            return []
        sch, tbl = parts[0].strip(), parts[1].strip()
        cap = max(1, min(int(limit), 100))
        qtbl = f"{self._quote_ident(sch)}.{self._quote_ident(tbl)}"
        sql = f"SELECT * FROM {qtbl} FETCH FIRST {cap} ROWS ONLY WITH UR"
        try:
            with self._connect() as conn:
                rows_raw = self._exec_fetchall(conn, sql)
                if not rows_raw:
                    return []
                col_sql = (
                    "SELECT COLNAME FROM SYSCAT.COLUMNS WHERE TABSCHEMA = ? AND TABNAME = ? "
                    "ORDER BY COLNO WITH UR"
                )
                cn = self._exec_fetchall(conn, col_sql, (sch.upper(), tbl.upper()))
                colnames = [str(c[0]) for c in cn]
                if not colnames and rows_raw:
                    colnames = [f"c{i}" for i in range(len(rows_raw[0]))]
                dict_rows: list[dict[str, Any]] = []
                for tup in rows_raw:
                    dict_rows.append(
                        {colnames[i]: tup[i] for i in range(min(len(colnames), len(tup)))}
                    )
                masked = mask_rows(dict_rows)
                return [SampleRow(data=r) for r in masked]
        except Exception as exc:
            logger.error("DB2 get_sample_data(%s) failed: %s", table_key, exc)
            return []

    def execute_explain(self, sql: str) -> ExplainResult:
        stmt = str(sql or "").strip()
        if not stmt:
            return ExplainResult(ok=False, plan="", error="Empty SQL.", dialect="db2")
        if not re.match(r"^\s*(SELECT|WITH)\b", stmt, re.IGNORECASE):
            return ExplainResult(
                ok=False,
                plan="",
                error="EXPLAIN supports SELECT / WITH statements only.",
                dialect="db2",
            )
        try:
            with self._connect() as conn:
                ibm_db = _import_ibm_db()
                explain_sql = f"EXPLAIN PLAN FOR {stmt}"
                q = ibm_db.prepare(conn, explain_sql)
                if q is None:
                    return ExplainResult(
                        ok=False,
                        plan="",
                        error=ibm_db.stmt_errormsg() or "EXPLAIN prepare failed",
                        dialect="db2",
                    )
                if ibm_db.execute(q) is None and ibm_db.stmt_error() != "":
                    err = ibm_db.stmt_errormsg() or "EXPLAIN execute failed"
                    ibm_db.free_stmt(q)
                    return ExplainResult(ok=False, plan="", error=err, dialect="db2")
                ibm_db.free_stmt(q)
                return ExplainResult(
                    ok=True,
                    plan="EXPLAIN PLAN FOR completed; inspect EXPLAIN_ARGUMENT / EXPLAIN_OPERATOR "
                    "catalog tables in this database for detailed access plans.",
                    dialect="db2",
                )
        except Exception as exc:
            msg = str(exc)
            logger.warning("DB2 EXPLAIN failed: %s", msg)
            return ExplainResult(ok=False, plan="", error=msg, dialect="db2")
