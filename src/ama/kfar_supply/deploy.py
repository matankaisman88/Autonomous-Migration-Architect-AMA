"""
Execute Kfar Supply DDL/DML on a live database.

Uses one transaction for T-SQL and DB2 (DDL + DML). Oracle runs DDL first (implicit commits),
then DML in a separate transaction; DDL cannot be rolled back on failure after partial apply.
"""

from __future__ import annotations

import logging
from typing import Callable

from ama.kfar_supply.spec import KFAR_TABLES, expected_column_sets
from ama.kfar_supply.sqlgen import deployment_statement_groups
from ama.mcp.base import SchemaProvider

logger = logging.getLogger(__name__)

LogFn = Callable[[str], None]


def validate_schema_matches_spec(provider: SchemaProvider) -> list[str]:
    """
    Ensure each Kfar table exists with expected column names (case-insensitive).
    Returns a list of human-readable issues; empty means OK.
    """
    exp = expected_column_sets()
    issues: list[str] = []
    for t in KFAR_TABLES:
        ts = provider.get_table_schema(t.full_key)
        if ts is None:
            issues.append(f"table missing or unreachable: {t.full_key}")
            continue
        got = {c.name.lower() for c in ts.columns}
        missing = exp[t.full_key] - got
        if missing:
            issues.append(f"{t.full_key}: missing columns {sorted(missing)}")
    return issues


def _noop_log(_: str) -> None:
    pass


def deploy_kfar_live(
    dialect: str,
    connection_string: str,
    *,
    log: LogFn | None = None,
    timeout_seconds: int = 120,
) -> None:
    """
    Run idempotent DDL + data reset + seed inserts.

    Raises RuntimeError on failure (after best-effort rollback for T-SQL / DB2 / Oracle DML).
    Never logs ``connection_string``.
    """
    lg = log or _noop_log
    _, groups = deployment_statement_groups(dialect)
    d = dialect.lower().strip()

    if d in ("tsql", "sqlserver"):
        _deploy_pyodbc_transaction(connection_string, groups, lg, timeout_seconds)
    elif d == "oracle":
        _deploy_oracle_groups(connection_string, groups, lg, timeout_seconds)
    elif d == "db2":
        _deploy_db2_transaction(connection_string, groups, lg)
    else:
        raise ValueError(f"unsupported deploy dialect: {dialect}")

    from ama.mcp.factory import get_schema_provider

    mode = "sqlserver" if d in ("tsql", "sqlserver") else d
    provider = get_schema_provider(mode=mode, connection_string=connection_string, timeout_seconds=timeout_seconds)
    try:
        issues = validate_schema_matches_spec(provider)
        if issues:
            raise RuntimeError("Schema validation failed: " + "; ".join(issues))
    finally:
        provider.close()


def _deploy_pyodbc_transaction(
    connection_string: str,
    groups: list[list[str]],
    lg: LogFn,
    timeout_seconds: int,
) -> None:
    try:
        import pyodbc
    except ImportError as exc:
        raise RuntimeError("pyodbc required for SQL Server Kfar deploy") from exc

    conn = pyodbc.connect(connection_string, timeout=timeout_seconds)
    conn.autocommit = False
    cur = conn.cursor()
    try:
        cur.timeout = timeout_seconds
    except Exception:
        pass
    try:
        for gi, group in enumerate(groups):
            lg(f"Executing SQL batch group {gi + 1}/{len(groups)} ({len(group)} statements)")
            for stmt in group:
                cur.execute(stmt)
        conn.commit()
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.exception("Kfar T-SQL deploy failed")
        raise RuntimeError(str(exc)) from exc
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _deploy_oracle_groups(
    connection_string: str,
    groups: list[list[str]],
    lg: LogFn,
    timeout_seconds: int,
) -> None:
    try:
        import oracledb
    except ImportError as exc:
        raise RuntimeError("oracledb required for Oracle Kfar deploy") from exc

    if len(groups) < 2:
        raise RuntimeError("internal: oracle deploy expects at least two statement groups")

    ddl_group, dml_group = groups[0], groups[1]
    conn = None
    try:
        cs = connection_string.strip()
        if "@" in cs and "/" in cs.split("@", 1)[0]:
            user_pass, dsn = cs.split("@", 1)
            user, password = user_pass.split("/", 1)
            conn = oracledb.connect(
                user=user,
                password=password,
                dsn=dsn,
                tcp_connect_timeout=timeout_seconds,
            )
        else:
            conn = oracledb.connect(dsn=cs, tcp_connect_timeout=timeout_seconds)
        cur = conn.cursor()
        for stmt in ddl_group:
            lg("Oracle DDL (idempotent create)")
            cur.execute(stmt)
        conn.commit()

        for stmt in dml_group:
            cur.execute(stmt)
        conn.commit()
    except Exception as exc:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        logger.exception("Kfar Oracle deploy failed")
        raise RuntimeError(str(exc)) from exc
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _deploy_db2_transaction(
    connection_string: str,
    groups: list[list[str]],
    lg: LogFn,
) -> None:
    try:
        import ibm_db
    except ImportError as exc:
        raise RuntimeError("ibm_db required for DB2 Kfar deploy") from exc

    conn = ibm_db.connect(connection_string, "", "")
    if conn is None:
        raise RuntimeError(ibm_db.conn_errormsg() or "ibm_db.connect failed")
    try:
        ac_off = getattr(ibm_db, "SQL_AUTOCOMMIT_OFF", 0)
        ibm_db.autocommit(conn, ac_off)
    except Exception:
        pass
    try:
        for gi, group in enumerate(groups):
            lg(f"DB2 batch group {gi + 1}/{len(groups)}")
            for stmt in group:
                q = ibm_db.prepare(conn, stmt)
                if q is None:
                    raise RuntimeError(ibm_db.stmt_errormsg() or "prepare failed")
                if ibm_db.execute(q) is None and ibm_db.stmt_error() != "":
                    err = ibm_db.stmt_errormsg() or "execute failed"
                    ibm_db.free_stmt(q)
                    raise RuntimeError(err)
                ibm_db.free_stmt(q)
        ibm_db.commit(conn)
    except Exception as exc:
        try:
            ibm_db.rollback(conn)
        except Exception:
            pass
        logger.exception("Kfar DB2 deploy failed")
        raise RuntimeError(str(exc)) from exc
    finally:
        try:
            ibm_db.close(conn)
        except Exception:
            pass
