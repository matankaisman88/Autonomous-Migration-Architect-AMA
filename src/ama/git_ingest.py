from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ama.sanitize import sanitize_text


@dataclass
class SqlFileHit:
    path: str
    score: float


_SQL_GLOB = ("*.sql", "*.SQL")


def iter_sql_files(roots: list[Path]) -> list[Path]:
    out: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for pattern in _SQL_GLOB:
            out.extend(root.rglob(pattern))
    return sorted(set(out))


def _score_content(text: str, schema: str, table: str) -> float:
    text = sanitize_text(text)
    s = schema.lower()
    t = table.lower()
    blob = text.lower()
    score = 0.0
    score += 4.0 * blob.count(f"{s}.{t}")
    score += 2.0 * blob.count(t)
    # FROM / JOIN clauses
    score += 1.5 * len(
        re.findall(rf"\b(from|join)\s+[`\"]?{re.escape(s)}[`\"]?\s*\.\s*[`\"]?{re.escape(t)}[`\"]?", blob)
    )
    return score


def scan_git_sql_roots(
    roots: list[Path],
    *,
    schema: str,
    table: str,
) -> tuple[float, list[SqlFileHit]]:
    total = 0.0
    hits: list[SqlFileHit] = []
    for path in iter_sql_files(roots):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        sc = _score_content(text, schema, table)
        if sc > 0:
            total += sc
            hits.append(SqlFileHit(path=str(path), score=sc))
    hits.sort(key=lambda h: h.score, reverse=True)
    return total, hits
