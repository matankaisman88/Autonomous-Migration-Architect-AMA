"""Tests for Jira / Confluence migration plan export."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

from ama.export import ConfluenceExportSink, ExportConfig, JiraExportSink, write_export
from ama.export.md_inline import adf_document_from_markdown, md_inline_to_html
from ama.planner import AutonomousPlanner
from ama.planner.models import MigrationPlan, MigrationWave, PlannedTable


def test_jira_export_wave_produces_epic_and_stories() -> None:
    """One wave with two tables yields one epic and two stories."""
    tables = [
        PlannedTable(
            full_name="sales.orders",
            business_domain="Finance",
            priority_score=90.0,
            query_count=10,
            rationale="ok",
            business_context="ctx",
            technical_note="tech",
        ),
        PlannedTable(
            full_name="sales.lines",
            business_domain="Finance",
            priority_score=50.0,
            query_count=5,
            rationale="ok",
        ),
    ]
    wave = MigrationWave(
        wave_id=1,
        name="Finance",
        tables=tables,
        business_rationale="Migrate finance first.",
        technical_rationale="Topo order.",
        metrics={
            "table_count": 2,
            "total_query_count": 15,
            "avg_priority_score": 70.0,
        },
    )
    plan = MigrationPlan(migration_context="sales.orders", waves=[wave], notes=[])
    config = ExportConfig(format="jira-json")
    out = JiraExportSink().write(plan, config)
    updates = out["issueUpdates"]
    assert len(updates) == 3
    assert updates[0]["fields"]["issuetype"]["name"] == "Epic"
    epic_summary = updates[0]["fields"]["summary"]
    assert epic_summary == "Wave 1: Finance"
    for story in updates[1:]:
        assert story["fields"]["issuetype"]["name"] == "Story"
        assert story["fields"]["customfield_10014"] == epic_summary
    assert "sales.orders" in updates[1]["fields"]["summary"]
    assert "sales.lines" in updates[2]["fields"]["summary"]


def test_jira_priority_mapping() -> None:
    """Scores 80 / 50 / 15 map to High / Medium / Low."""
    config = ExportConfig(format="jira-json")
    scores = [80.0, 50.0, 15.0]
    expected = ["High", "Medium", "Low"]
    for score, exp in zip(scores, expected, strict=True):
        pt = PlannedTable(
            full_name="x.t",
            business_domain="D",
            priority_score=score,
            query_count=1,
        )
        wave = MigrationWave(wave_id=1, name="D", tables=[pt])
        plan = MigrationPlan(waves=[wave])
        out = JiraExportSink().write(plan, config)
        story = out["issueUpdates"][1]
        assert story["fields"]["priority"]["name"] == exp


def test_jira_plan_notes_produce_task() -> None:
    """Non-empty plan.notes add a Task issue."""
    plan = MigrationPlan(
        waves=[],
        notes=["Remember to validate cutover window."],
    )
    config = ExportConfig(format="jira-json")
    out = JiraExportSink().write(plan, config)
    tasks = [
        u for u in out["issueUpdates"] if u["fields"]["issuetype"]["name"] == "Task"
    ]
    assert len(tasks) == 1
    assert "Notes" in tasks[0]["fields"]["summary"]


def test_confluence_export_contains_table_names() -> None:
    """HTML includes wave headings and table names in code tags."""
    w1 = MigrationWave(
        wave_id=1,
        name="Alpha",
        tables=[
            PlannedTable(
                full_name="dbo.orders",
                business_domain="Ops",
                priority_score=80.0,
                query_count=3,
            ),
        ],
    )
    w2 = MigrationWave(
        wave_id=2,
        name="Beta",
        tables=[
            PlannedTable(
                full_name="finance.pay",
                business_domain="Finance",
                priority_score=40.0,
                query_count=2,
            ),
        ],
    )
    plan = MigrationPlan(migration_context="dbo.orders", waves=[w1, w2])
    html_out = ConfluenceExportSink().write(plan, ExportConfig(format="confluence"))
    assert "<h2>Wave 1: Alpha</h2>" in html_out
    assert "<h2>Wave 2: Beta</h2>" in html_out
    assert "<code>dbo.orders</code>" in html_out
    assert "<code>finance.pay</code>" in html_out


def test_md_inline_to_html_bold_and_code() -> None:
    """** and ` in rationale become <strong> and <code>; plain text is escaped."""
    assert md_inline_to_html("**Risk** on `dbo.orders` & <x>") == (
        "<strong>Risk</strong> on <code>dbo.orders</code> &amp; &lt;x&gt;"
    )


def test_adf_document_from_markdown_bold_and_code() -> None:
    """Jira ADF uses strong and code marks for ** and ` spans."""
    doc = adf_document_from_markdown("See **Finance** domain, table `finance.pay`.")
    assert doc["type"] == "doc"
    para = doc["content"][0]
    assert para["type"] == "paragraph"
    nodes = para["content"]
    assert nodes[0] == {"type": "text", "text": "See "}
    assert nodes[1]["type"] == "text" and nodes[1]["text"] == "Finance"
    assert nodes[1]["marks"] == [{"type": "strong"}]
    assert any(
        n.get("type") == "text" and n.get("text") == "finance.pay" and n.get("marks") == [{"type": "code"}]
        for n in nodes
    )


def test_confluence_escapes_special_chars() -> None:
    """User-controlled text is HTML-escaped in output."""
    pt = PlannedTable(
        full_name="finance.invoices",
        business_domain="Fin",
        priority_score=10.0,
        query_count=1,
        technical_note='Use <JOIN> carefully & check "quotes"',
    )
    wave = MigrationWave(wave_id=1, name="W", tables=[pt])
    plan = MigrationPlan(waves=[wave])
    html_out = ConfluenceExportSink().write(plan, ExportConfig(format="confluence"))
    assert "&lt;JOIN&gt;" in html_out
    assert "&amp;" in html_out


def test_write_export_jira_csv_roundtrip(tmp_path: Path) -> None:
    """Default Jira format writes UTF-8 BOM CSV with inventory rows."""
    plan = MigrationPlan(waves=[], notes=[])
    report = {
        "discovery": {
            "inventory": [
                {
                    "full_name": "a.b",
                    "business_domain": "D",
                    "priority_score": 50.0,
                    "query_count": 1,
                    "status": "ok",
                },
            ],
        },
    }
    p = tmp_path / "out.csv"
    write_export(plan, ExportConfig(format="jira"), p, report=report)
    assert p.is_file()
    raw = p.read_bytes()
    assert raw[:3] == b"\xef\xbb\xbf"
    text = p.read_text(encoding="utf-8-sig")
    assert "Migrate: a.b" in text
    assert "Project Key" not in text
    assert text.startswith('"Summary"')
    parsed = list(csv.reader(io.StringIO(text)))
    assert len(parsed) >= 2
    desc_i = parsed[0].index("Description")
    assert "\n" not in parsed[1][desc_i] and "\r" not in parsed[1][desc_i]


def test_write_export_jira_json_roundtrip(tmp_path: Path) -> None:
    """jira-json export writes bulk-create JSON with issueUpdates."""
    plan = MigrationPlan(
        waves=[
            MigrationWave(
                wave_id=1,
                name="X",
                tables=[
                    PlannedTable(
                        full_name="a.b",
                        business_domain="D",
                        priority_score=50.0,
                        query_count=1,
                    ),
                ],
            ),
        ],
    )
    p = tmp_path / "out.json"
    write_export(plan, ExportConfig(format="jira-json"), p, report=None)
    assert p.is_file()
    data = json.loads(p.read_text(encoding="utf-8"))
    assert isinstance(data["issueUpdates"], list)


def test_write_export_confluence_roundtrip(tmp_path: Path) -> None:
    """Confluence export writes HTML starting with h1."""
    plan = MigrationPlan(waves=[], notes=[])
    p = tmp_path / "out.html"
    write_export(plan, ExportConfig(format="confluence"), p)
    text = p.read_text(encoding="utf-8")
    assert text.startswith("<h1>")


def test_no_lineage_export_still_works() -> None:
    """Planner output without lineage in the report still exports."""
    report = {
        "migration_context": "dbo.orders",
        "discovery": {
            "inventory": [
                {
                    "full_name": "dbo.orders",
                    "business_domain": "Operations",
                    "priority_score": 100.0,
                    "query_count": 50,
                    "status": "active",
                },
            ],
        },
    }
    plan = AutonomousPlanner().plan_from_report(report)
    config = ExportConfig(format="jira-json")
    out = JiraExportSink().write(plan, config)
    assert len(out["issueUpdates"]) >= 1
    html_out = ConfluenceExportSink().write(plan, ExportConfig(format="confluence"))
    assert "<h1>" in html_out
