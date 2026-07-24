#!/usr/bin/env python3
"""
Execute benchmark SELECT batches from ``dirty_kfar_queries.sql`` against SQL Server.

AMA Live connection does **not** read ``.sql`` files — it exports SQL that already ran
and was captured in **Query Store** (or the plan cache). Run this script after generating
benchmark SQL and before re-running Live extraction.

Usage (from repo root)::

    python tools/generate_kfar_benchmark.py --count 1000
    python tools/execute_kfar_benchmark.py
    # Then re-run Live connection in the UI (log end date = today, all schemas or dbo+finance+logistics)

Optional direct JSONL export (skips Query Store; for ``ama-ingest run --sql-logs``)::

    python tools/generate_kfar_benchmark.py --count 1000 --jsonl-out live_data/kfar_benchmark/sql_logs/prod.jsonl
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SQL = ROOT / "tools" / "dirty_kfar_queries.sql"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from ama.mcp.extraction import split_tsql_batch  # noqa: E402

_GO_BATCH_RE = re.compile(r"^\s*GO\s*(?:\r?\n|$)", re.I | re.M)


def _strip_leading_sql_comments(sql: str) -> str:
    """Drop SSMS-style leading /* */ and -- line comments."""
    body = sql.strip()
    while body:
        if body.startswith("/*"):
            end = body.find("*/")
            if end == -1:
                break
            body = body[end + 2 :].lstrip()
            continue
        if body.startswith("--"):
            nl = body.find("\n")
            if nl == -1:
                return ""
            body = body[nl + 1 :].lstrip()
            continue
        break
    return body


def _is_benchmark_select(sql: str) -> bool:
    head = _strip_leading_sql_comments(sql).upper()
    return head.startswith("SELECT") or head.startswith("WITH")


def _load_queries(sql_path: Path) -> list[str]:
    text = sql_path.read_text(encoding="utf-8")
    batches = _GO_BATCH_RE.split(text)
    queries: list[str] = []
    for batch in batches:
        chunk = batch.strip()
        if not chunk or chunk.startswith("/*") or chunk.upper().startswith("USE "):
            continue
        if chunk.startswith("-- Query"):
            chunk = chunk.split("\n", 1)[1].strip() if "\n" in chunk else ""
        if not chunk:
            continue
        for stmt in split_tsql_batch(chunk):
            if _is_benchmark_select(stmt):
                queries.append(stmt.strip())
    return queries


def _ensure_query_store(conn_str: str) -> None:
    import pyodbc  # type: ignore

    conn = pyodbc.connect(conn_str, timeout=10)
    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(
            """
            ALTER DATABASE CURRENT SET QUERY_STORE = ON (
                OPERATION_MODE = READ_WRITE,
                QUERY_CAPTURE_MODE = ALL,
                SIZE_BASED_CLEANUP_MODE = OFF
            );
            """
        )
    finally:
        conn.close()


def _tag_query(sql: str, query_num: int) -> str:
    """Unique batch marker so Query Store / AMA dedupe keep distinct entries."""
    marker = f"/* ama-bench-q{query_num:05d} */"
    body = sql.strip()
    if body.startswith("/*") and "ama-bench-q" in body[:40]:
        return body
    return f"{marker}\n{body}"


def execute_queries(
    queries: list[str],
    *,
    conn_str: str,
    progress_every: int = 100,
) -> tuple[int, int]:
    import pyodbc  # type: ignore

    _ensure_query_store(conn_str)
    conn = pyodbc.connect(conn_str, timeout=30)
    ok = 0
    failed = 0
    t0 = time.perf_counter()
    try:
        conn.autocommit = True
        cur = conn.cursor()
        for i, sql in enumerate(queries, start=1):
            tagged = _tag_query(sql, i)
            try:
                cur.execute(tagged)
                cur.fetchall()
                ok += 1
            except Exception as exc:
                failed += 1
                if failed <= 5:
                    print(f"[FAIL] Query {i}: {exc}", flush=True)
            if progress_every and i % progress_every == 0:
                print(f"  ... executed {i:,}/{len(queries):,}", flush=True)
    finally:
        conn.close()
    elapsed = time.perf_counter() - t0
    print(f"Executed {ok:,} ok, {failed:,} failed in {elapsed:.1f}s", flush=True)
    return ok, failed


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Execute dirty_kfar_queries.sql against SQL Server for Query Store capture."
    )
    p.add_argument("--sql", type=Path, default=DEFAULT_SQL, help="Benchmark .sql file")
    p.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Execute only the first N queries (0 = all)",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except ImportError:
        pass

    args = _parse_args(argv)
    conn_str = os.environ.get("MSSQL_CONNECTION_STRING", "").strip()
    if not conn_str:
        print("error: MSSQL_CONNECTION_STRING is not set (run tools/setup_dev_mssql.py first)", file=sys.stderr)
        return 2

    sql_path = args.sql.resolve()
    if not sql_path.is_file():
        print(f"error: SQL file not found: {sql_path}", file=sys.stderr)
        return 2

    queries = _load_queries(sql_path)
    if args.limit and args.limit > 0:
        queries = queries[: args.limit]
    if not queries:
        print(f"error: no SELECT batches found in {sql_path}", file=sys.stderr)
        return 2

    print(f"Loaded {len(queries):,} queries from {sql_path}", flush=True)
    ok, failed = execute_queries(queries, conn_str=conn_str)
    if failed:
        print(
            "warning: some batches failed — re-run generate_kfar_benchmark.py or fix SQL",
            file=sys.stderr,
        )
        return 1 if ok == 0 else 0
    print("Done. Re-run Live connection extraction (log end date = today).", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
