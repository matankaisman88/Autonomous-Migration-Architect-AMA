"""Stage 1: Co-occurrence mining — find RTL/ASCII pairs in the same SQL query."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from ama.parsing.backend import default_parse_backend
from ama.sanitize import has_rtl_script, normalize_sql_identifier, sanitize_sql_text

# Adjacent RTL→English pairs like ``[סטטוס], order_id`` are common in legacy SQL but
# almost never a semantic glossary mapping; prefer the next non-key column instead.
_SKIP_ADJACENT_DDL: frozenset[str] = frozenset({"order_id"})


def _column_names_ordered(chunks: list[dict[str, dict[str, int]]]) -> list[str]:
    """Column names in parse/SELECT-list order (may repeat) for RTL↔DDL adjacency pairing."""
    out: list[str] = []
    for chunk in chunks:
        for tbl_cols in chunk.values():
            for role_col in tbl_cols:
                col = role_col.split(":", 1)[-1] if ":" in role_col else role_col
                n = normalize_sql_identifier(col)
                if n:
                    out.append(n)
    return out


def mine_cooccurrences(
    sql_log_paths: list[Path],
    ddl_columns: list[str],
    *,
    env_filter: str | None = "prod",
    max_records: int = 0,
) -> dict[str, dict[str, int]]:
    """
    Stream SQL log JSONL files and count (rtl_token, ddl_column) co-occurrences.

    Returns: { rtl_token -> { ddl_column -> count } }

    Counts **adjacent** pairs where a Hebrew/RTL column is **immediately followed**
    by an English DDL name in the SELECT list (``... [סכום], amount ...``), avoiding
    spurious links from ``customer_id, [סכום]``-style orderings.
    """
    ddl_set = {normalize_sql_identifier(c) for c in ddl_columns if c}
    # rtl_token (normalized) -> { ddl_col (normalized) -> count }
    pairs: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    backend = default_parse_backend()
    total = 0

    for path in sql_log_paths:
        if not path.is_file():
            continue
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue

                if env_filter:
                    rec_env = str(rec.get("env") or "").lower()
                    if rec_env and rec_env != env_filter.lower():
                        continue

                sql_raw = rec.get("sql") or rec.get("query") or rec.get("statement") or ""
                if not sql_raw:
                    continue

                dialect = rec.get("dialect") if isinstance(rec.get("dialect"), str) else None
                pr = backend.parse(sanitize_sql_text(str(sql_raw)), dialect=dialect)
                if not pr.chunks:
                    continue

                cols = _column_names_ordered(pr.chunks)
                for i in range(len(cols) - 1):
                    a, b = cols[i], cols[i + 1]
                    # Hebrew/RTL token immediately followed by English DDL (bilingual SELECT pattern)
                    if (
                        has_rtl_script(a)
                        and not has_rtl_script(b)
                        and b in ddl_set
                        and b not in _SKIP_ADJACENT_DDL
                    ):
                        pairs[a][b] += 1

                total += 1
                if max_records and total >= max_records:
                    return {k: dict(v) for k, v in pairs.items()}

    return {k: dict(v) for k, v in pairs.items()}


def cooccurrence_candidates(
    pairs: dict[str, dict[str, int]],
    *,
    min_count: int = 3,
    top_k: int = 3,
) -> dict[str, list[tuple[str, int]]]:
    """
    From raw co-occurrence counts, produce ranked candidates per RTL token.

    Returns: { rtl_token -> [(ddl_column, count), ...] } sorted by count desc.
    Only includes RTL tokens with at least one DDL match occurring >= min_count times.
    """
    out: dict[str, list[tuple[str, int]]] = {}
    for rtl, ddl_counts in pairs.items():
        ranked = sorted(ddl_counts.items(), key=lambda x: (-x[1], x[0]))
        filtered = [(d, c) for d, c in ranked if c >= min_count]
        if filtered:
            out[rtl] = filtered[:top_k]
    return out
