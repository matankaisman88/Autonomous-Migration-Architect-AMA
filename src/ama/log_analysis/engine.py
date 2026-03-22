"""
Log Analysis Engine — orchestrates streaming reads and SQL parse telemetry.

This module does **not** load whole log files into memory; it uses
:func:`ama.sql_pipeline.iter_sql_log_records` and the same per-record ingest path
as discovery for consistent ``SqlIngestionTelemetry``.
"""

from __future__ import annotations

import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ama.log_analysis.config import LogAnalysisConfig
from ama.sql_pipeline import (
    SqlIngestionTelemetry,
    TableColumnStats,
    _ingest_one_record_discovery,
    iter_sql_log_records,
)


@dataclass
class LogAnalysisSummary:
    """Aggregated result of a log scan (serializable)."""

    files: list[str]
    total_rows: int
    telemetry: dict[str, Any]
    distinct_tables: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class LogAnalysisEngine:
    """
    Facade for analyzing legacy SQL JSONL logs.

    Uses discovery-style ingestion to populate parse telemetry and count distinct
    table keys seen in successfully parsed chunks (streaming).
    """

    def __init__(self, config: LogAnalysisConfig | None = None) -> None:
        self._config = config or LogAnalysisConfig()

    def analyze_paths(
        self,
        paths: list[Path],
        *,
        progress: bool = False,
    ) -> LogAnalysisSummary:
        """
        Stream all given files and return telemetry + approximate table cardinality.

        ``per_table`` stats are merged for counting; only the set of table names is
        summarized in ``distinct_tables``.
        """
        cfg = self._config
        env = cfg.effective_env()
        telemetry = SqlIngestionTelemetry()
        per_table: dict[str, TableColumnStats] = defaultdict(TableColumnStats)

        files_str = [str(p) for p in paths]
        total = 0
        for path in paths:
            max_r = cfg.max_records_per_file
            for rec in iter_sql_log_records(path, max_records=max_r):
                total += 1
                if progress and total % cfg.progress_every == 0:
                    print(
                        f"log_analysis: processed {total} records (last file {path.name})",
                        file=sys.stderr,
                    )
                _ingest_one_record_discovery(
                    rec,
                    env=env,
                    per_table=per_table,
                    telemetry=telemetry,
                    lineage=None,
                )

        telemetry.total_rows = total
        return LogAnalysisSummary(
            files=files_str,
            total_rows=total,
            telemetry=telemetry.to_dict(),
            distinct_tables=len(per_table),
        )
