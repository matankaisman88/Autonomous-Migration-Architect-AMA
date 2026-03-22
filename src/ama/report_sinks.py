"""
Report output sinks — JSON / Markdown / Excel (ReportSink protocol for CLI extensions).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol

from ama.reports import (
    render_markdown_summary,
    resolve_report_output_path,
    write_excel_report,
    write_report_file,
)


class ReportSink(Protocol):
    def write(
        self,
        report: dict[str, Any],
        *,
        target: str,
        out_spec: str,
        cwd: Path,
    ) -> Path: ...


class JsonReportSink:
    def write(
        self,
        report: dict[str, Any],
        *,
        target: str,
        out_spec: str,
        cwd: Path,
    ) -> Path:
        out_path = resolve_report_output_path(
            out_spec,
            table_full_name=target,
            extension=".json",
            cwd=cwd,
        )
        write_report_file(out_path, json.dumps(report, indent=2, ensure_ascii=False))
        return out_path


class MarkdownReportSink:
    def write(
        self,
        report: dict[str, Any],
        *,
        target: str,
        out_spec: str,
        cwd: Path,
    ) -> Path:
        md_out = render_markdown_summary(report)
        out_path = resolve_report_output_path(
            out_spec,
            table_full_name=target,
            extension=".md",
            cwd=cwd,
        )
        write_report_file(out_path, md_out)
        return out_path


class ExcelReportSink:
    def write(
        self,
        report: dict[str, Any],
        *,
        target: str,
        out_spec: str,
        cwd: Path,
    ) -> Path:
        out_path = resolve_report_output_path(
            out_spec,
            table_full_name=target,
            extension=".xlsx",
            cwd=cwd,
        )
        write_excel_report(report, out_path)
        return out_path
