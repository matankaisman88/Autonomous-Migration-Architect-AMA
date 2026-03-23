"""
Jira bulk-create JSON export for migration plans (ADF issue descriptions).
"""

from __future__ import annotations

import re
from typing import Any

from ama.export.config import ExportConfig
from ama.export.md_inline import adf_document_from_markdown
from ama.planner.models import MigrationPlan, MigrationWave, PlannedTable


def _priority_band(score: float) -> str:
    """Map numeric priority score to high / medium / low band."""
    if score >= 70.0:
        return "high"
    if score >= 40.0:
        return "medium"
    return "low"


def _jira_priority_name(score: float, config: ExportConfig) -> str:
    """Resolve Jira priority display name from score and config map."""
    band = _priority_band(score)
    return config.jira_priority_map.get(band, config.jira_priority_map.get("medium", "Medium"))


def _truncate(text: str, max_len: int) -> str:
    """Truncate plain text to max_len characters."""
    if len(text) <= max_len:
        return text
    return text[:max_len]


def _domain_label(wave: MigrationWave) -> str:
    """Produce a slug like ``finance`` for Jira labels."""
    if wave.tables:
        dom = wave.tables[0].business_domain.strip()
    else:
        dom = wave.name.strip()
    slug = re.sub(r"[^a-z0-9]+", "-", dom.lower()).strip("-")
    return slug or "unclassified"


def _table_label(full_name: str) -> str:
    """Sanitize ``schema.table`` for Jira labels (dots to hyphens, lowercase)."""
    return "table-" + full_name.replace(".", "-").lower()


def _epic_metrics_suffix(wave: MigrationWave) -> str:
    """Trailing metrics line for the epic description."""
    m = wave.metrics
    n = int(m.get("table_count") or len(wave.tables))
    total_q = int(m.get("total_query_count") or 0)
    avg = float(m.get("avg_priority_score") or 0.0)
    return f"Metrics: {n} tables, {total_q} queries, avg priority {avg:.2f}%"


def _story_body(table: PlannedTable, config: ExportConfig) -> str:
    """Combine technical and business notes for a story; fall back to rationale."""
    tn = (table.technical_note or "").strip()
    bc = (table.business_context or "").strip()
    if tn and bc:
        raw = f"{tn}\n{bc}"
    elif tn:
        raw = tn
    elif bc:
        raw = bc
    else:
        raw = (table.rationale or "").strip()
    return _truncate(raw, config.max_description_chars)


def _wave_epic_priority(wave: MigrationWave, config: ExportConfig) -> str:
    """Epic priority from the highest table score in the wave."""
    if not wave.tables:
        return _jira_priority_name(0.0, config)
    top = max(float(t.priority_score) for t in wave.tables)
    return _jira_priority_name(top, config)


class JiraExportSink:
    """Serializes a :class:`MigrationPlan` to Jira bulk-create JSON."""

    def write(self, plan: MigrationPlan, config: ExportConfig) -> dict[str, Any]:
        """Return the bulk-create envelope dict (``issueUpdates``)."""
        issue_updates: list[dict[str, Any]] = []
        domain_slug_cache: dict[int, str] = {}

        for wave in plan.waves:
            epic_summary = f"{config.epic_prefix} {wave.wave_id}: {wave.name}"
            br = _truncate(
                (wave.business_rationale or "").strip(),
                config.max_description_chars,
            )
            metrics_line = _epic_metrics_suffix(wave)
            epic_desc = br
            if epic_desc:
                epic_desc = f"{epic_desc}\n{metrics_line}"
            else:
                epic_desc = metrics_line

            dom_slug = _domain_label(wave)
            domain_slug_cache[wave.wave_id] = dom_slug

            issue_updates.append(
                {
                    "fields": {
                        "project": {"key": config.project_key},
                        "issuetype": {"name": "Epic"},
                        "summary": epic_summary,
                        "description": adf_document_from_markdown(epic_desc),
                        "priority": {"name": _wave_epic_priority(wave, config)},
                        "labels": [
                            "ama-migration",
                            f"wave-{wave.wave_id}",
                            f"domain-{dom_slug}",
                        ],
                        "customfield_10014": epic_summary,
                    },
                },
            )

            for table in wave.tables:
                story_summary = (
                    f"Migrate {table.full_name} "
                    f"(priority: {table.priority_score:.2f})"
                )
                issue_updates.append(
                    {
                        "fields": {
                            "project": {"key": config.project_key},
                            "issuetype": {"name": "Story"},
                            "summary": story_summary,
                            "description": adf_document_from_markdown(_story_body(table, config)),
                            "priority": {
                                "name": _jira_priority_name(
                                    float(table.priority_score),
                                    config,
                                ),
                            },
                            "labels": [
                                "ama-migration",
                                f"wave-{wave.wave_id}",
                                f"domain-{domain_slug_cache[wave.wave_id]}",
                                _table_label(table.full_name),
                            ],
                            "customfield_10014": epic_summary,
                        },
                    },
                )

        if plan.notes:
            notes_text = _truncate("\n".join(plan.notes), config.max_description_chars)
            issue_updates.append(
                {
                    "fields": {
                        "project": {"key": config.project_key},
                        "issuetype": {"name": "Task"},
                        "summary": "AMA Migration Plan Notes",
                        "description": adf_document_from_markdown(notes_text),
                        "priority": {"name": config.jira_priority_map.get("medium", "Medium")},
                        "labels": ["ama-migration", "plan-notes"],
                    },
                },
            )

        return {"issueUpdates": issue_updates}
