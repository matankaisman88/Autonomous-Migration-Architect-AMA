from __future__ import annotations

from dataclasses import dataclass

from ama.git_ingest import SqlFileHit
from ama.sql_pipeline import ColumnStats, TableColumnStats


@dataclass
class ColumnImportance:
    column: str
    log_weight: float
    comms_weight: float
    git_weight: float
    importance_score: float
    dead_candidate: bool


def _total_log_weight(cs: ColumnStats) -> float:
    return float(
        cs.select + cs.where + cs.join_on + cs.group_by + cs.order_by
    )


def compute_importance_v0(
    sql_stats: TableColumnStats,
    *,
    comms_score: float,
    comms_chunks: int,
    git_score: float,
    git_hits: list[SqlFileHit],
    w_log: float = 1.0,
    w_comms: float = 0.15,
    w_git: float = 0.1,
    dead_threshold: float = 0.05,
) -> list[ColumnImportance]:
    """
    importance = w_log * norm(log) + w_comms * norm(comms) + w_git * norm(git)

    Comms/git signals are table-level; distributed evenly across columns that
    appear in logs, with a small floor for unmapped columns.
    """
    cols = sorted(sql_stats.columns.keys())
    if not cols:
        return [
            ColumnImportance(
                column="(no_columns_in_sql_logs)",
                log_weight=0.0,
                comms_weight=min(1.0, comms_score / (10.0 + comms_score)),
                git_weight=min(1.0, git_score / (10.0 + git_score)),
                importance_score=w_comms * min(1.0, comms_score / (10.0 + comms_score))
                + w_git * min(1.0, git_score / (10.0 + git_score)),
                dead_candidate=False,
            )
        ]

    log_vals = [_total_log_weight(sql_stats.columns[c]) for c in cols]
    max_log = max(log_vals) if log_vals else 1.0
    if max_log <= 0:
        max_log = 1.0

    # Table-level social/code signals normalized to [0,1]
    comms_norm = min(1.0, comms_score / (10.0 + comms_score))
    if comms_chunks:
        comms_norm = min(1.0, comms_norm + 0.05 * min(comms_chunks, 5))

    gh_sum = sum(h.score for h in git_hits) or 0.0
    git_norm = min(1.0, git_score / (10.0 + git_score + gh_sum * 0.01))

    n_active = sum(1 for v in log_vals if v > 0)
    per_col_social = comms_norm / max(n_active, 1)
    per_col_git = git_norm / max(n_active, 1)

    out: list[ColumnImportance] = []
    for c, lv in zip(cols, log_vals, strict=True):
        log_n = lv / max_log
        has_log = lv > 0
        cw = per_col_social if has_log else comms_norm * 0.25
        gw = per_col_git if has_log else git_norm * 0.25
        score = w_log * log_n + w_comms * cw + w_git * gw
        dead = (log_n <= dead_threshold) and comms_norm < 0.1 and git_norm < 0.1
        out.append(
            ColumnImportance(
                column=c,
                log_weight=log_n,
                comms_weight=cw,
                git_weight=gw,
                importance_score=score,
                dead_candidate=dead,
            )
        )

    out.sort(key=lambda r: r.importance_score, reverse=True)
    return out
