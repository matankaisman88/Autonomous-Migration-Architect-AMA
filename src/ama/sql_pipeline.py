from __future__ import annotations

import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

from ama.lineage import LineageGraph
from ama.parsing.backend import ParseResult, default_parse_backend
from ama.sanitize import normalize_sql_identifier, sanitize_sql_text

# Safeguards: very large logs or pathological lines should warn instead of failing silently or OOMing.
_MAX_SQL_LOG_BYTES = 500 * 1024 * 1024
_MAX_JSONL_LINE_CHARS = 2 * 1024 * 1024
_MAX_JSON_WARN_LINES = 8
_MAX_COLUMN_COPEERS = 48


def _warn_sql_log_path(path: Path) -> None:
    try:
        sz = path.stat().st_size
    except OSError as e:
        print(f"warning: cannot read SQL log size ({e}): {path}", file=sys.stderr)
        return
    if sz > _MAX_SQL_LOG_BYTES:
        print(
            f"warning: SQL log is very large ({sz / (1024 * 1024):.1f} MiB); ingestion may be slow: {path}",
            file=sys.stderr,
        )


@dataclass
class SqlIngestionTelemetry:
    """Counters for SQL log processing (never aborts stream)."""

    total_rows: int = 0
    parse_ok: int = 0
    regex_fallback: int = 0
    skipped_empty_sql: int = 0
    skipped_env_mismatch: int = 0
    unparsed_no_chunks: int = 0

    def record_parse_result(self, pr: ParseResult, *, had_sql_field: bool) -> None:
        if pr.mode == "sqlglot":
            self.parse_ok += 1
        elif pr.mode == "regex":
            self.regex_fallback += 1
        elif pr.mode == "skipped_empty":
            if had_sql_field:
                self.skipped_empty_sql += 1
        elif pr.mode == "empty":
            if had_sql_field:
                self.unparsed_no_chunks += 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_rows": self.total_rows,
            "parse_ok": self.parse_ok,
            "regex_fallback": self.regex_fallback,
            "skipped_empty": self.skipped_empty_sql,
            "skipped_empty_sql": self.skipped_empty_sql,
            "skipped_env_mismatch": self.skipped_env_mismatch,
            "unparsed_no_chunks": self.unparsed_no_chunks,
        }


@dataclass
class ColumnStats:
    select: int = 0
    where: int = 0
    join_on: int = 0
    group_by: int = 0
    order_by: int = 0


@dataclass
class TableColumnStats:
    columns: dict[str, ColumnStats] = field(default_factory=lambda: defaultdict(ColumnStats))
    query_count: int = 0
    # Normalized column name -> peers seen in same query (bounded).
    co_peers: dict[str, set[str]] = field(default_factory=dict)

    def merge_cooccurrence(self, column_names: list[str]) -> None:
        norm = [normalize_sql_identifier(c) or c for c in column_names if c]
        norm = list(dict.fromkeys(norm))
        for i, a in enumerate(norm):
            for b in norm[i + 1 :]:
                if a == b:
                    continue
                sa = self.co_peers.setdefault(a, set())
                if len(sa) < _MAX_COLUMN_COPEERS:
                    sa.add(b)
                sb = self.co_peers.setdefault(b, set())
                if len(sb) < _MAX_COLUMN_COPEERS:
                    sb.add(a)


def _column_names_from_role_map(role_cols: dict[str, int]) -> list[str]:
    out: list[str] = []
    for rk in role_cols:
        _, _, col = rk.partition(":")
        if col and col != "*":
            out.append(col)
    return list(dict.fromkeys(out))


def _table_matches_target(tl: str, target_keys: set[str], short: str) -> bool:
    tl_norm = normalize_sql_identifier(tl)
    tl_cmp = tl_norm.lower() if all(ord(c) < 128 for c in tl_norm) else tl_norm
    short_cmp = short.lower() if short else short
    if tl_cmp in target_keys:
        return True
    if tl_cmp == short_cmp:
        return True
    return tl_cmp.endswith("." + short_cmp)


def _merge_role_counts_into_stats(
    flat: dict[str, dict[str, int]],
    target_keys: set[str],
    short: str,
    stats: TableColumnStats,
) -> None:
    for tbl, role_cols in flat.items():
        tl = tbl
        if not _table_matches_target(tl, target_keys, short):
            continue
        for rk, n in role_cols.items():
            role, _, col = rk.partition(":")
            if not col or col == "*":
                continue
            cs = stats.columns[col]
            if role == "select":
                cs.select += n
            elif role == "where":
                cs.where += n
            elif role == "join_on":
                cs.join_on += n
            elif role == "group_by":
                cs.group_by += n
            elif role == "order_by":
                cs.order_by += n


def parse_sql_query(sql_text: str, dialect: str | None = None) -> tuple[list[dict[str, dict[str, int]]], bool]:
    """Backward-compatible: returns (chunks, True iff SQLGlot produced chunks)."""
    pr = default_parse_backend().parse(sql_text, dialect=dialect)
    return pr.chunks, pr.mode == "sqlglot"


def _ingest_one_record_discovery(
    rec: dict[str, Any],
    *,
    env: str | None,
    per_table: dict[str, TableColumnStats],
    telemetry: SqlIngestionTelemetry | None = None,
    lineage: LineageGraph | None = None,
) -> None:
    if env and rec.get("env") and str(rec["env"]).lower() != str(env).lower():
        if telemetry is not None:
            telemetry.skipped_env_mismatch += 1
        return
    sql_raw = rec.get("sql") or rec.get("query") or rec.get("statement")
    had_sql = bool(sql_raw and isinstance(sql_raw, str))
    if not had_sql:
        if telemetry is not None:
            telemetry.skipped_empty_sql += 1
        return
    sql = sanitize_sql_text(str(sql_raw))
    dialect = rec.get("dialect") if isinstance(rec.get("dialect"), str) else None
    pr = default_parse_backend().parse(sql, dialect=dialect)
    if telemetry is not None:
        telemetry.record_parse_result(pr, had_sql_field=True)
    chunks = pr.chunks
    if not chunks:
        return

    if lineage is not None:
        lineage.ingest_parse_result(pr)

    touched: set[str] = set()
    for flat in chunks:
        for tbl in flat:
            touched.add(tbl)
    for tbl in touched:
        per_table[tbl].query_count += 1

    for flat in chunks:
        for tbl, role_cols in flat.items():
            merge_flat_into_table_stats(flat, tbl, per_table[tbl])
            per_table[tbl].merge_cooccurrence(_column_names_from_role_map(role_cols))


def _ingest_one_record_target(
    rec: dict[str, Any],
    *,
    env: str | None,
    tkeys: set[str],
    short: str,
    out: TableColumnStats,
    telemetry: SqlIngestionTelemetry | None = None,
) -> None:
    if env and rec.get("env") and str(rec["env"]).lower() != str(env).lower():
        if telemetry is not None:
            telemetry.skipped_env_mismatch += 1
        return
    sql_raw = rec.get("sql") or rec.get("query") or rec.get("statement")
    if not sql_raw or not isinstance(sql_raw, str):
        if telemetry is not None:
            telemetry.skipped_empty_sql += 1
        return
    sql = sanitize_sql_text(sql_raw)
    dialect = rec.get("dialect") if isinstance(rec.get("dialect"), str) else None
    pr = default_parse_backend().parse(sql, dialect=dialect)
    if telemetry is not None:
        telemetry.record_parse_result(pr, had_sql_field=True)
    chunks = pr.chunks
    if not chunks:
        return

    matched = False
    for flat in chunks:
        for tbl in flat:
            if _table_matches_target(tbl, tkeys, short):
                matched = True
                break
        if matched:
            break
    if not matched:
        return

    out.query_count += 1
    for flat in chunks:
        _merge_role_counts_into_stats(flat, tkeys, short, out)
        for tbl, role_cols in flat.items():
            if _table_matches_target(tbl, tkeys, short):
                out.merge_cooccurrence(_column_names_from_role_map(role_cols))


def iter_sql_log_records(
    path: Path,
    *,
    max_records: int | None = None,
) -> Iterator[dict[str, Any]]:
    """
    Stream JSONL records (one dict per line). Does not load the whole file into memory.
    ``max_records`` stops after that many successfully parsed records (invalid lines do not count).
    """
    _warn_sql_log_path(path)
    bad_json = 0
    yielded = 0
    with path.open(encoding="utf-8", errors="replace") as f:
        for line_no, line in enumerate(f, 1):
            if max_records is not None and yielded >= max_records:
                break
            if len(line) > _MAX_JSONL_LINE_CHARS:
                print(
                    f"warning: skipping oversized line ({len(line)} chars) at {line_no} in {path}",
                    file=sys.stderr,
                )
                continue
            line = line.replace("\x00", "").strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                bad_json += 1
                if bad_json <= _MAX_JSON_WARN_LINES:
                    print(
                        f"warning: invalid JSON on line {line_no} in {path} (skipped)",
                        file=sys.stderr,
                    )
                continue
            yield rec
            yielded += 1
    if bad_json > _MAX_JSON_WARN_LINES:
        print(
            f"warning: {bad_json - _MAX_JSON_WARN_LINES} additional invalid JSON lines skipped in {path}",
            file=sys.stderr,
        )


def iter_sql_log_record_batches(
    path: Path,
    *,
    batch_size: int = 5000,
    max_records: int | None = None,
) -> Iterator[list[dict[str, Any]]]:
    """
    Yield bounded batches of parsed records for batched processing and progress reporting.
    Still streams the underlying file; only one batch is held at a time.
    """
    batch: list[dict[str, Any]] = []
    for rec in iter_sql_log_records(path, max_records=max_records):
        batch.append(rec)
        if len(batch) >= batch_size:
            yield batch
            batch = []
    if batch:
        yield batch


def table_matches_target(table_key: str, target_full_table: str) -> bool:
    """True if a qualified table key refers to the same object as target_full_table (schema.table)."""
    tkeys = _target_keys(target_full_table)
    short = normalize_sql_identifier(target_full_table.split(".")[-1])
    return _table_matches_target(table_key, tkeys, short)


def table_matches_scope(table_key: str, migration_context_reference: str) -> bool:
    """Same as :func:`table_matches_target` — preferred name for system-wide migration runs."""
    return table_matches_target(table_key, migration_context_reference)


def _target_keys(target_full_table: str) -> set[str]:
    schema, _, table = target_full_table.partition(".")
    t = normalize_sql_identifier(table or target_full_table)
    s = normalize_sql_identifier(schema) if schema else ""
    keys: set[str] = {t, normalize_sql_identifier(target_full_table)}
    if s:
        keys.add(f"{s}.{t}")
    return keys


def process_sql_log_file(
    path: Path,
    *,
    target_full_table: str,
    env: str | None = "prod",
    batch_size: int | None = None,
    progress: bool = False,
    max_records: int | None = None,
    telemetry: SqlIngestionTelemetry | None = None,
) -> TableColumnStats:
    out = TableColumnStats()
    tkeys = _target_keys(target_full_table)
    short = normalize_sql_identifier(target_full_table.split(".")[-1])

    def _one(rec: dict[str, Any]) -> None:
        if telemetry is not None:
            telemetry.total_rows += 1
        _ingest_one_record_target(
            rec, env=env, tkeys=tkeys, short=short, out=out, telemetry=telemetry
        )

    if batch_size is None:
        stream = iter_sql_log_records(path, max_records=max_records)
        if progress:
            try:
                from tqdm import tqdm

                stream = tqdm(stream, desc=path.name, unit=" rec", unit_scale=True)
            except ImportError:
                pass
        for rec in stream:
            _one(rec)
        return out

    batches = iter_sql_log_record_batches(path, batch_size=batch_size, max_records=max_records)
    if progress:
        try:
            from tqdm import tqdm

            batches = tqdm(batches, desc=path.name, unit="batch")
        except ImportError:
            pass
    for batch in batches:
        for rec in batch:
            _one(rec)

    return out


def merge_stats(into: TableColumnStats, other: TableColumnStats) -> None:
    into.query_count += other.query_count
    for col, oc in other.columns.items():
        ic = into.columns[col]
        ic.select += oc.select
        ic.where += oc.where
        ic.join_on += oc.join_on
        ic.group_by += oc.group_by
        ic.order_by += oc.order_by
    for k, peers in other.co_peers.items():
        tgt = into.co_peers.setdefault(k, set())
        for p in peers:
            if len(tgt) < _MAX_COLUMN_COPEERS:
                tgt.add(p)


def merge_flat_into_table_stats(
    flat: dict[str, dict[str, int]],
    table_key: str,
    stats: TableColumnStats,
) -> None:
    """Merge role counts for one table from a flat chunk into stats (discovery / multi-table)."""
    role_cols = flat.get(table_key)
    if not role_cols:
        return
    for rk, n in role_cols.items():
        role, _, col = rk.partition(":")
        if not col or col == "*":
            continue
        cs = stats.columns[col]
        if role == "select":
            cs.select += n
        elif role == "where":
            cs.where += n
        elif role == "join_on":
            cs.join_on += n
        elif role == "group_by":
            cs.group_by += n
        elif role == "order_by":
            cs.order_by += n


def process_sql_log_file_discovery(
    path: Path,
    *,
    env: str | None = "prod",
    batch_size: int | None = None,
    progress: bool = False,
    max_records: int | None = None,
    on_batch_complete: Callable[[int], None] | None = None,
    records_counter: list[int] | None = None,
    telemetry: SqlIngestionTelemetry | None = None,
    lineage: LineageGraph | None = None,
) -> dict[str, TableColumnStats]:
    """
    Aggregate column stats per qualified table key (no target filter).
    Streaming JSONL read — never loads the full log into memory.

    ``batch_size`` — if set, accumulate records in chunks (e.g. 5000) for progress / GC cadence.
    ``progress`` — tqdm progress (requires tqdm).
    ``on_batch_complete`` — optional callback after each batch ``(batch_index,)`` for monitors.
    ``records_counter`` — optional single-element list incremented per record consumed.
    """
    per_table: dict[str, TableColumnStats] = defaultdict(TableColumnStats)

    def _one(rec: dict[str, Any]) -> None:
        if records_counter is not None:
            records_counter[0] += 1
        if telemetry is not None:
            telemetry.total_rows += 1
        _ingest_one_record_discovery(
            rec, env=env, per_table=per_table, telemetry=telemetry, lineage=lineage
        )

    if batch_size is None:
        stream = iter_sql_log_records(path, max_records=max_records)
        if progress:
            try:
                from tqdm import tqdm

                stream = tqdm(stream, desc=path.name, unit=" rec", unit_scale=True)
            except ImportError:
                pass
        for rec in stream:
            _one(rec)
        return dict(per_table)

    batches = iter_sql_log_record_batches(path, batch_size=batch_size, max_records=max_records)
    if progress:
        try:
            from tqdm import tqdm

            batches = tqdm(batches, desc=path.name, unit="batch")
        except ImportError:
            pass
    for bi, batch in enumerate(batches):
        for rec in batch:
            _one(rec)
        if on_batch_complete is not None:
            on_batch_complete(bi)

    return dict(per_table)


def run_sql_logs_discovery_pipeline(
    sql_log_paths: list[Path],
    *,
    env: str | None = "prod",
    batch_size: int | None = None,
    progress: bool = False,
    max_records_per_file: int | None = None,
    on_batch_complete: Callable[[int], None] | None = None,
    records_counter: list[int] | None = None,
    telemetry: SqlIngestionTelemetry | None = None,
    lineage: LineageGraph | None = None,
) -> dict[str, TableColumnStats]:
    total: dict[str, TableColumnStats] = defaultdict(TableColumnStats)
    for p in sql_log_paths:
        part = process_sql_log_file_discovery(
            p,
            env=env,
            batch_size=batch_size,
            progress=progress and len(sql_log_paths) == 1,
            max_records=max_records_per_file,
            on_batch_complete=on_batch_complete,
            records_counter=records_counter,
            telemetry=telemetry,
            lineage=lineage,
        )
        for k, st in part.items():
            merge_stats(total[k], st)
    return dict(total)


def run_sql_logs_pipeline(
    sql_log_paths: list[Path],
    *,
    target_full_table: str,
    env: str | None = "prod",
    batch_size: int | None = None,
    progress: bool = False,
    max_records_per_file: int | None = None,
    telemetry: SqlIngestionTelemetry | None = None,
) -> TableColumnStats:
    total = TableColumnStats()
    for p in sql_log_paths:
        other = process_sql_log_file(
            p,
            target_full_table=target_full_table,
            env=env,
            batch_size=batch_size,
            progress=progress and len(sql_log_paths) == 1,
            max_records=max_records_per_file,
            telemetry=telemetry,
        )
        merge_stats(total, other)
    return total
