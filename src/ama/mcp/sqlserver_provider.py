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
from datetime import date, datetime, timedelta, time
from typing import Any, Generator

from ama.mcp.base import ColumnInfo, ExplainResult, SampleRow, SchemaProvider, TableSchema
from ama.mcp.extraction import (
    LogExtractionResult,
    expand_plan_cache_sql_rows,
    filter_application_sql_texts,
    is_noise_or_system_sql,
    normalize_sql_for_dedupe,
    redact_sql_literals,
)
from ama.mcp.pii import mask_rows

logger = logging.getLogger(__name__)

# INFORMATION_SCHEMA exposes these; they are not migration targets.
_SQLSERVER_SYSTEM_SCHEMAS = ("sys", "INFORMATION_SCHEMA", "guest")


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

            # Best-effort: request a read-only intent, but some environments/versions
            # can timeout with ApplicationIntent=ReadOnly. If that happens, retry
            # without it so discovery still works.
            conn_str_base = str(self._conn_str).strip()
            conn_str_aug = self._augment_connection_string_readonly(conn_str_base)
            # Try the caller's connection string first to avoid environments
            # where ApplicationIntent=ReadOnly can cause slow/hanging logins.
            attempts = [conn_str_base]
            if conn_str_aug != conn_str_base:
                attempts.append(conn_str_aug)

            last_exc: Exception | None = None
            for i, attempt_conn_str in enumerate(attempts):
                try:
                    conn = pyodbc.connect(attempt_conn_str, timeout=self._timeout)
                    conn.autocommit = True
                    yield conn
                    return
                except Exception as exc:
                    last_exc = exc
                    if i == 0 and len(attempts) > 1:
                        logger.warning(
                            "SQLServer connect failed; retrying with ApplicationIntent=ReadOnly. Error: %s",
                            exc,
                        )
                        continue

                    logger.warning("SQLServer connection failed: %s", exc)
                    raise

            # If we got here, all attempts failed.
            if last_exc is not None:
                raise last_exc
        except Exception as exc:
            raise
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception as exc:
                    logger.warning("SQLServer: failed to close connection: %s", exc)

    def _set_query_timeout(self, cursor: Any) -> None:
        # pyodbc uses cursor.timeout for query execution timeout (seconds).
        try:
            cursor.timeout = self._timeout
        except Exception as exc:
            logger.warning("SQLServer: failed to set cursor timeout: %s", exc)

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
        schema_name, table_name = parts[0].strip(), parts[1].strip()
        if not schema_name or not table_name:
            return None

        try:
            with self._connect() as conn:
                cur = conn.cursor()
                self._set_query_timeout(cur)
                ts = self._fetch_table_schema_on_cursor(cur, schema_name, table_name)
                if ts is None:
                    return None
                self._apply_foreign_keys(cur, [schema_name], {ts.full_name: ts})
                return ts
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
                masked = mask_rows(raw_rows)
                return [SampleRow(data=row) for row in masked]
        except Exception as exc:
            logger.error("SQLServer get_sample_data(%s) failed: %s", table_key, exc)
            return []

    def _is_query_store_enabled(self, cur: Any) -> bool:
        try:
            cur.execute("SELECT actual_state_desc FROM sys.database_query_store_options")
            row = cur.fetchone()
            if row is None:
                return False
            state = str(row[0] or "").upper()
            return state in {"READ_WRITE", "READ_ONLY"}
        except Exception as exc:
            logger.warning("SQLServer Query Store probe failed: %s", exc)
            return False

    @staticmethod
    def _resolve_log_date_range(
        start_date: str | None,
        end_date: str | None,
    ) -> tuple[datetime, datetime]:
        today = date.today()
        end_d = date.fromisoformat(end_date) if end_date else today
        start_d = date.fromisoformat(start_date) if start_date else (end_d - timedelta(days=6))
        start_dt = datetime.combine(start_d, time.min)
        end_dt = datetime.combine(end_d, time(23, 59, 59, 997000))
        return start_dt, end_dt

    @staticmethod
    def _is_noise_sql(text: str) -> bool:
        return is_noise_or_system_sql(text)

    @staticmethod
    def _fetch_pool_size(max_rows: int) -> int:
        cap = max(1, min(int(max_rows), 50_000))
        return min(max(cap * 20, 1000), 50_000)

    @staticmethod
    def _dedupe_sql_texts(raw_sqls: list[str], max_rows: int) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for sql in raw_sqls:
            key = normalize_sql_for_dedupe(sql)
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(sql.strip())
            if len(out) >= max_rows:
                break
        return out

    def _fetch_table_schema_on_cursor(
        self,
        cur: Any,
        schema_name: str,
        table_name: str,
    ) -> TableSchema | None:
        cur.execute(
            """
            SELECT
                c.COLUMN_NAME,
                c.DATA_TYPE,
                c.IS_NULLABLE,
                CASE WHEN pk.COLUMN_NAME IS NULL THEN 0 ELSE 1 END AS IS_PRIMARY_KEY
            FROM INFORMATION_SCHEMA.COLUMNS c
            LEFT JOIN (
                SELECT kcu.COLUMN_NAME
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
            cols.append(
                ColumnInfo(
                    name=str(r[0]),
                    data_type=str(r[1]),
                    nullable=str(r[2]).upper() == "YES",
                    primary_key=bool(int(r[3])) if r[3] is not None else False,
                )
            )
        return TableSchema(
            schema_name=schema_name,
            table_name=table_name,
            columns=cols,
            row_count_estimate=None,
        )

    def _apply_foreign_keys(
        self,
        cur: Any,
        schemas: list[str],
        tables: dict[str, TableSchema],
    ) -> None:
        """Attach ``foreign_key_ref`` on columns from sys.foreign_keys (read-only)."""
        if not schemas or not tables:
            return
        placeholders = ",".join("?" for _ in schemas)
        try:
            cur.execute(
                f"""
                SELECT
                    fk_s.name AS fk_schema,
                    fk_t.name AS fk_table,
                    fk_c.name AS fk_column,
                    pk_s.name AS pk_schema,
                    pk_t.name AS pk_table,
                    pk_c.name AS pk_column
                FROM sys.foreign_keys fk
                INNER JOIN sys.foreign_key_columns fkc
                    ON fk.object_id = fkc.constraint_object_id
                INNER JOIN sys.tables fk_t
                    ON fkc.parent_object_id = fk_t.object_id
                INNER JOIN sys.schemas fk_s
                    ON fk_t.schema_id = fk_s.schema_id
                INNER JOIN sys.columns fk_c
                    ON fkc.parent_object_id = fk_c.object_id
                   AND fkc.parent_column_id = fk_c.column_id
                INNER JOIN sys.tables pk_t
                    ON fkc.referenced_object_id = pk_t.object_id
                INNER JOIN sys.schemas pk_s
                    ON pk_t.schema_id = pk_s.schema_id
                INNER JOIN sys.columns pk_c
                    ON fkc.referenced_object_id = pk_c.object_id
                   AND fkc.referenced_column_id = pk_c.column_id
                WHERE fk_s.name IN ({placeholders})
                """,
                tuple(schemas),
            )
            for row in cur.fetchall():
                fk_key = f"{row[0]}.{row[1]}"
                ts = tables.get(fk_key)
                if ts is None:
                    continue
                fk_col = str(row[2])
                ref = f"{row[3]}.{row[4]}.{row[5]}"
                for col in ts.columns:
                    if col.name == fk_col:
                        col.foreign_key_ref = ref
                        break
        except Exception as exc:
            logger.warning("SQLServer foreign key introspection failed: %s", exc)

    def extract_ddl(
        self,
        schemas: list[str] | None = None,
        *,
        all_schemas: bool = False,
    ) -> dict[str, TableSchema]:
        """
        Read-only DDL introspection for BASE TABLEs.

        Pass ``all_schemas=True`` to export every user schema in the database, or
        a explicit ``schemas`` list (e.g. ``["dbo", "finance"]``).
        """
        if all_schemas:
            norm_schemas: list[str] = []
        else:
            norm_schemas = []
            for s in schemas or []:
                ss = str(s or "").strip()
                if ss and ss not in norm_schemas:
                    norm_schemas.append(ss)
            if not norm_schemas:
                return {}

        out: dict[str, TableSchema] = {}
        try:
            with self._connect() as conn:
                cur = conn.cursor()
                self._set_query_timeout(cur)
                if all_schemas:
                    placeholders = ", ".join("?" for _ in _SQLSERVER_SYSTEM_SCHEMAS)
                    cur.execute(
                        f"""
                        SELECT TABLE_SCHEMA, TABLE_NAME
                        FROM INFORMATION_SCHEMA.TABLES
                        WHERE TABLE_TYPE = 'BASE TABLE'
                          AND TABLE_SCHEMA NOT IN ({placeholders})
                        ORDER BY TABLE_SCHEMA, TABLE_NAME
                        """,
                        _SQLSERVER_SYSTEM_SCHEMAS,
                    )
                    table_rows = cur.fetchall()
                else:
                    table_rows = []
                    for schema_name in norm_schemas:
                        cur.execute(
                            """
                            SELECT TABLE_SCHEMA, TABLE_NAME
                            FROM INFORMATION_SCHEMA.TABLES
                            WHERE TABLE_TYPE = 'BASE TABLE'
                              AND TABLE_SCHEMA = ?
                            ORDER BY TABLE_SCHEMA, TABLE_NAME
                            """,
                            (schema_name,),
                        )
                        table_rows.extend(cur.fetchall())
                for row in table_rows:
                    sch, tbl = str(row[0]), str(row[1])
                    ts = self._fetch_table_schema_on_cursor(cur, sch, tbl)
                    if ts is not None and ts.columns:
                        out[ts.full_name] = ts
                if out:
                    fk_schemas = sorted({k.split(".", 1)[0] for k in out if "." in k})
                    self._apply_foreign_keys(cur, fk_schemas, out)
        except Exception as exc:
            logger.error("SQLServer extract_ddl failed: %s", exc)
            return out
        return out

    def _extract_logs_query_store(
        self,
        cur: Any,
        start_dt: datetime,
        end_dt: datetime,
        max_rows: int,
    ) -> list[str]:
        cur.execute(
            """
            SELECT TOP (?)
                qt.query_sql_text AS sql_text
            FROM sys.query_store_runtime_stats rs
            INNER JOIN sys.query_store_plan p
                ON rs.plan_id = p.plan_id
            INNER JOIN sys.query_store_query q
                ON p.query_id = q.query_id
            INNER JOIN sys.query_store_query_text qt
                ON q.query_text_id = qt.query_text_id
            WHERE rs.last_execution_time >= ?
              AND rs.last_execution_time <= ?
              AND qt.query_sql_text IS NOT NULL
              AND LTRIM(RTRIM(qt.query_sql_text)) <> ''
            ORDER BY rs.last_execution_time DESC
            """,
            (max_rows, start_dt, end_dt),
        )
        return [str(r[0]) for r in cur.fetchall() if r and r[0] is not None]

    def _extract_logs_plan_cache(
        self,
        cur: Any,
        max_rows: int,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[str]:
        params: list[Any] = [max_rows]
        time_clauses = ""
        if since is not None:
            time_clauses += " AND qs.last_execution_time >= ?"
            params.append(since)
        if until is not None:
            time_clauses += " AND qs.last_execution_time <= ?"
            params.append(until)
        cur.execute(
            f"""
            SELECT TOP (?)
                st.text AS batch_text
            FROM sys.dm_exec_query_stats AS qs
            CROSS APPLY sys.dm_exec_sql_text(qs.sql_handle) AS st
            WHERE st.text IS NOT NULL
              AND LTRIM(RTRIM(st.text)) <> ''
              {time_clauses}
            ORDER BY qs.last_execution_time DESC
            """,
            tuple(params),
        )
        raw_batches = [str(r[0]) for r in cur.fetchall() if r and r[0] is not None]
        expanded = expand_plan_cache_sql_rows(raw_batches)
        return [s for s in expanded if not self._is_noise_sql(s)]

    @staticmethod
    def _read_server_identity(cur: Any) -> tuple[str, str]:
        try:
            cur.execute("SELECT CAST(@@SERVERNAME AS NVARCHAR(256)), CAST(DB_NAME() AS NVARCHAR(256))")
            row = cur.fetchone()
            if row:
                return str(row[0] or ""), str(row[1] or "")
        except Exception:
            pass
        return "", ""

    def extract_logs(
        self,
        start_date: str | None,
        end_date: str | None,
        max_rows: int,
        schemas: list[str] | None = None,
    ) -> LogExtractionResult:
        """
        Extract SQL text for JSONL export. Query Store first, plan cache fallback.

        Dedupe uses pre-redaction text; redaction applied before returning records.
        Filters out system-catalog and non-application SQL; prefers queries referencing
        the requested schemas.
        """
        cap = max(1, min(int(max_rows), 50_000))
        fetch_pool = self._fetch_pool_size(cap)
        norm_schemas = [str(s).strip() for s in (schemas or []) if str(s or "").strip()]
        warnings: list[str] = ["SQL literals redacted before export"]
        stats: dict[str, int | str] = {}
        records: list[dict[str, str]] = []
        source = "plan_cache"
        date_range_applied = False

        try:
            start_dt, end_dt = self._resolve_log_date_range(start_date, end_date)
        except ValueError as exc:
            logger.error("SQLServer extract_logs invalid date range: %s", exc)
            return LogExtractionResult(
                records=[],
                source=source,
                date_range_applied=False,
                warnings=[str(exc), *warnings],
                stats=stats,
            )

        try:
            with self._connect() as conn:
                cur = conn.cursor()
                self._set_query_timeout(cur)
                server_name, db_name = self._read_server_identity(cur)
                if server_name or db_name:
                    stats["server_name"] = server_name
                    stats["database_name"] = db_name
                    warnings.insert(
                        0,
                        f"Connected to server={server_name or '?'} database={db_name or '?'}",
                    )

                raw_sqls: list[str] = []
                qs_enabled = self._is_query_store_enabled(cur)
                if qs_enabled:
                    source = "query_store"
                    date_range_applied = True
                    raw_sqls = self._extract_logs_query_store(cur, start_dt, end_dt, fetch_pool)
                    stats["query_store_raw"] = len(raw_sqls)
                else:
                    warnings.append(
                        "Query Store unavailable or disabled — using plan cache "
                        f"({start_dt.date().isoformat()} through {end_dt.date().isoformat()})"
                    )
                    raw_sqls = self._extract_logs_plan_cache(
                        cur, fetch_pool, since=start_dt, until=end_dt
                    )
                    stats["plan_cache_batches"] = len(raw_sqls)
                    date_range_applied = True

                filtered, skipped = filter_application_sql_texts(raw_sqls, norm_schemas, cap)
                stats["after_schema_filter"] = len(filtered)
                if skipped:
                    warnings.append(
                        f"Skipped {skipped} system/internal SQL batch(es) — keeping application SQL only"
                    )

                sparse_qs = qs_enabled and len(filtered) < cap
                if sparse_qs:
                    warnings.append(
                        f"Query Store returned {len(filtered)} application SQL batch(es) "
                        f"(below max_log_rows={cap}) — supplementing from plan cache "
                        f"({start_dt.date().isoformat()} through {end_dt.date().isoformat()})"
                    )
                    plan_raw = self._extract_logs_plan_cache(
                        cur, fetch_pool, since=start_dt, until=end_dt
                    )
                    stats["plan_cache_batches"] = len(plan_raw)
                    plan_filtered, plan_skipped = filter_application_sql_texts(
                        plan_raw, norm_schemas, cap
                    )
                    if plan_skipped:
                        warnings.append(
                            f"Plan cache: skipped {plan_skipped} system/internal SQL batch(es)"
                        )
                    if plan_filtered:
                        merged: list[str] = []
                        seen_keys: set[str] = set()
                        for sql in filtered + plan_filtered:
                            key = normalize_sql_for_dedupe(sql)
                            if not key or key in seen_keys:
                                continue
                            seen_keys.add(key)
                            merged.append(sql)
                        filtered = merged
                        stats["after_schema_filter"] = len(filtered)
                        source = "query_store+plan_cache"
                        date_range_applied = True
                elif not filtered and qs_enabled:
                    warnings.append(
                        "Query Store had no application SQL for the requested schemas/date range "
                        f"— supplementing from plan cache "
                        f"({start_dt.date().isoformat()} through {end_dt.date().isoformat()})"
                    )
                    plan_raw = self._extract_logs_plan_cache(
                        cur, fetch_pool, since=start_dt, until=end_dt
                    )
                    stats["plan_cache_batches"] = len(plan_raw)
                    filtered, plan_skipped = filter_application_sql_texts(plan_raw, norm_schemas, cap)
                    stats["after_schema_filter"] = len(filtered)
                    if plan_skipped:
                        warnings.append(
                            f"Plan cache: skipped {plan_skipped} system/internal SQL batch(es)"
                        )
                    if filtered:
                        source = "query_store+plan_cache"
                        date_range_applied = True

                deduped = self._dedupe_sql_texts(filtered, cap)
                stats["unique_after_dedupe"] = len(deduped)
                stats["exported_rows"] = len(deduped)
                if len(deduped) < len(filtered):
                    warnings.append(
                        f"Deduped {len(filtered) - len(deduped)} duplicate SQL text(s) — "
                        "re-running identical queries does not increase row count; use distinct SQL "
                        "or SSMS batches with unique comments (see tools/kfar_test_queries.sql)"
                    )
                for sql in deduped:
                    redacted = redact_sql_literals(sql)
                    if not redacted.strip():
                        continue
                    records.append({"env": "prod", "dialect": "tsql", "sql": redacted})
        except Exception as exc:
            logger.error("SQLServer extract_logs failed: %s", exc)
            warnings.append(f"extract_logs failed: {exc}")

        if not records and not any("failed" in w for w in warnings):
            schema_hint = ", ".join(norm_schemas) if norm_schemas else "requested schemas"
            warnings.append(
                f"No application SQL found for {schema_hint} — run representative workloads "
                "against those tables or widen the log date range"
            )

        return LogExtractionResult(
            records=records,
            source=source,
            date_range_applied=date_range_applied,
            warnings=warnings,
            stats=stats,
        )

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

