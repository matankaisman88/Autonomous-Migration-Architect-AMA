"""
Write migration plan exports to disk (Jira CSV, Jira bulk JSON, or Confluence HTML).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ama.export.config import ExportConfig
from ama.export.confluence_sink import ConfluenceExportSink
from ama.export.jira_csv import write_jira_csv_from_report
from ama.export.jira_sink import JiraExportSink
from ama.planner.models import MigrationPlan


def write_export(
    plan: MigrationPlan,
    config: ExportConfig,
    out_path: Path,
    *,
    report: dict[str, Any] | None = None,
) -> Path:
    """
    Serialize ``plan`` to ``out_path`` per ``config.format`` and return ``out_path``.

    ``report`` is required when ``format == \"jira\"`` (CSV from ``discovery.inventory``).
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if config.format == "jira":
        if report is None:
            raise ValueError(
                'Jira CSV export requires the AMA report dict (use write_export(..., report=report)).',
            )
        write_jira_csv_from_report(report, out_path)
    elif config.format == "jira-json":
        payload = JiraExportSink().write(plan, config)
        out_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    else:
        html_body = ConfluenceExportSink().write(plan, config)
        out_path.write_text(html_body, encoding="utf-8")
    return out_path
