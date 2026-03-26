"""
AMA Streamlit dashboard: Business Translator, domain deep dives, search, HITL.

Loads the same JSON as Excel (`ama-ingest run --format json`). Optional sidecar
`<report>.hitl.json` stores approve/reject decisions for review rows.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import queue
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import uuid
import time
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from ama.data_quality import run_dq_suite
from ama.hitl_apply import apply_hitl_to_report
from ama.planner import AutonomousPlanner
from ama.business_logic import (
    build_business_glossary_entries,
    build_impact_readiness_scatter_rows,
    domain_data_health_filtered,
    enrich_executive_risk_hotspots,
    expand_concept_query,
    group_glossary_entries,
    review_row_signature,
    semantic_concept_search,
)
from ama.ui.lineage_widget import (
    PYVIS_INSTALL_HINT,
    broken_tables_from_report,
    lineage_subgraph_html,
    pyvis_available,
)
from ama.ui.report_helpers import (
    _domain_for_table,
    _high_risk_tables,
    _inventory_df,
    _merge_rows_for_filters,
    filter_domain_matrix_rows,
    filter_glossary_grouped,
    filter_risk_hotspots,
    filter_semantic_search_results,
    filter_merge_buckets_by_inventory,
    inventory_allowed_tables,
    load_report_json,
    pct_confirmed_filtered,
)
from ama.env_resolver import get_env, get_openai_model, has_openai_api_key
from ama.migration_agent.engine import init_state as init_migration_agent_state, run_agent_turn
from ama.migration_agent import agent_tools as migration_agent_tools
from ama.dbt_migration.service import (
    analyze_model_risk_and_scenarios,
    apply_ai_fix_from_checkpoint,
    generate_synthetic_data_for_model,
    poll_generate_dbt_checkpoint_a_job,
    propose_sql_patch_from_chat,
    run_wave_stress_test,
    start_generate_dbt_checkpoint_a_job,
)
from ama.dbt_migration.runner import approve_checkpoint_b_sql, reject_checkpoint_b_to_dlq
from ama.dbt_migration.writer import _write_model_files
from ama.bulk_runner import (
    _BULK_JOBS,
    _BULK_JOBS_LOCK,
    _bulk_job_clear,
    _bulk_job_load,
    _bulk_job_write,
    _run_bulk_job,
)
from ama.scale_engine import evaluate_batch, queue_emoji

# Tab groups — single source of truth for Analysis vs Execution (see test_tab_group_structure).
ANALYSIS_TABS: list[str] = [
    "Overview",
    "Domains",
    "Tables",
    "Glossary",
    "Lineage",
    "Bulk Migration",
    "Planner",
    "Ask the data",
    "Data quality",
]
EXECUTION_TABS: list[str] = ["Migration Agent", "HITL Review"]

SCALE_ENGINE_CACHE_KEY = "scale_engine_result"
SCALE_ENGINE_THRESHOLD_KEY = "scale_engine_thresholds"
SCALE_ENGINE_REPORT_KEY = "scale_engine_report_identity"
LAUNCHPAD_EXPANDED_KEY = "launchpad_expanded"
AGENT_TAB_ACTIVE_KEY = "agent_tab_active"
AGENT_PREFILL_KEY = "agent_prefill"
MIGRATION_NOTICE_KEY = "migration_notice"

_DBT_JINJA_BLOCK_RE = re.compile(r"({{.*?}}|{%-?.*?-%})", flags=re.DOTALL)


def _get_or_compute_scale_result(
    report: dict[str, Any],
    conf_floor: int,
    crit_ceil: int,
) -> Any:
    cached_thresholds = st.session_state.get(SCALE_ENGINE_THRESHOLD_KEY)
    cached_result = st.session_state.get(SCALE_ENGINE_CACHE_KEY)
    if cached_result is None or cached_thresholds != (conf_floor, crit_ceil):
        result = evaluate_batch(
            report,
            dry_run=True,
            conf_floor=conf_floor,
            crit_ceil=crit_ceil,
        )
        st.session_state[SCALE_ENGINE_CACHE_KEY] = result
        st.session_state[SCALE_ENGINE_THRESHOLD_KEY] = (conf_floor, crit_ceil)
    return st.session_state[SCALE_ENGINE_CACHE_KEY]


def _set_agent_prefill(prompt: str) -> None:
    st.session_state[AGENT_TAB_ACTIVE_KEY] = True
    st.session_state[AGENT_PREFILL_KEY] = str(prompt or "").strip()


def _pending_write_from_result(result: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(result, dict):
        return None
    pw = result.get("pending_write")
    if isinstance(pw, dict):
        return pw
    wg = result.get("write_gate")
    if isinstance(wg, dict) and isinstance(wg.get("pending_write"), dict):
        return wg.get("pending_write")
    return None


def _queue_table_pending_write(
    *,
    table_key: str,
    report: dict[str, Any],
    report_path: Path | None,
    dialect: str,
) -> bool:
    if report_path is None:
        st.warning("Set a report path first to enable per-table migration.")
        return False
    prop = migration_agent_tools.propose_dbt_model(
        report=report,
        report_path=report_path,
        table=str(table_key),
        dialect=str(dialect or "duckdb"),
        glossary_path=None,
    )
    pending = _pending_write_from_result(prop)
    if not isinstance(pending, dict):
        pending = {
            "model_name": str(prop.get("model_name") or str(table_key).replace(".", "_")),
            "sql": str(prop.get("sql") or ""),
            "schema_yml": str(prop.get("schema_yml") or ""),
        }
    st.session_state[f"pending_write_{table_key}"] = pending
    return True


def _resolve_output_dir(report_path: Path | None, dbt_project_dir: Path) -> Path:
    mig = st.session_state.get("migration_agent") or {}
    raw = str(mig.get("output_dir") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return (dbt_project_dir / "models" / "ama_generated").resolve()


def _mark_table_migrated(table_key: str, model_name: str) -> None:
    key = str(table_key)
    migrated = set(st.session_state.get("migrated_tables", []))
    migrated.add(key)
    st.session_state["migrated_tables"] = sorted(migrated)
    st.session_state.pop(f"pending_write_{key}", None)
    st.session_state.pop(f"pending_write_{key}_fix", None)
    st.session_state.pop(f"tbl_review_open_{key}", None)
    st.session_state.pop(f"tbl_explain_result_{key}", None)
    if str(st.session_state.get("tbl_pick_main") or "") == key:
        st.session_state["tbl_pick_main"] = ""
    st.session_state[MIGRATION_NOTICE_KEY] = (
        f"Migration finished: `{key}` (`{model_name}`) marked as migrated."
    )


def _get_bulk_job_state(*, dbt_project_dir: Path) -> tuple[str, dict[str, Any] | None]:
    job_id = str(st.session_state.get("bulk_job_id") or "")
    if not job_id:
        return "", None
    job: dict[str, Any] | None = None
    with _BULK_JOBS_LOCK:
        raw = _BULK_JOBS.get(job_id)
        if isinstance(raw, dict):
            job = dict(raw)
    if job is None:
        job = _bulk_job_load(dbt_project_dir=dbt_project_dir, job_id=job_id)
    return job_id, job


def _apply_bulk_completion_once(
    *, dbt_project_dir: Path
) -> tuple[str, dict[str, Any] | None, bool]:
    job_id, job = _get_bulk_job_state(dbt_project_dir=dbt_project_dir)
    if not job_id or not isinstance(job, dict):
        return job_id, job, False
    if str(job.get("status") or "") != "done":
        return job_id, job, False
    refresh_needed = False
    success_tables = [str(x) for x in (job.get("success") or []) if str(x).strip()]
    if success_tables:
        migrated = set(st.session_state.get("migrated_tables", []))
        before = set(migrated)
        migrated.update(success_tables)
        if migrated != before:
            st.session_state["migrated_tables"] = sorted(migrated)
            refresh_needed = True
    if bool(job.get("completion_applied")):
        return job_id, job, refresh_needed
    if success_tables:
        st.session_state[MIGRATION_NOTICE_KEY] = (
            f"Bulk migration finished: {len(success_tables)} table(s) marked as migrated."
        )
        refresh_needed = True
    job["completion_applied"] = True
    with _BULK_JOBS_LOCK:
        _BULK_JOBS[job_id] = dict(job)
    _bulk_job_write(dbt_project_dir=dbt_project_dir, job_id=job_id, payload=dict(job))
    return job_id, job, refresh_needed


def _render_pending_write_panel(
    table_key: str,
    *,
    report_path: Path | None,
    dbt_project_dir: Path,
    output_dir: Path,
    dbt_target: str | None = None,
    key_prefix: str = "pending",
) -> None:
    state_key = f"pending_write_{table_key}"
    pending = st.session_state.get(state_key)
    if not isinstance(pending, dict):
        return
    with st.expander(f"⏳ Awaiting Approval — {table_key}", expanded=True):
        model_name = str(pending.get("model_name") or str(table_key).replace(".", "_"))
        sql = str(pending.get("sql") or "")
        schema_yml = str(pending.get("schema_yml") or "")
        st.code(sql, language="sql")
        st.code(str(pending.get("schema_yml") or ""), language="yaml")
        out_dir = _resolve_output_dir(report_path, dbt_project_dir) if output_dir is None else output_dir
        st.caption(f"Target output directory: `{out_dir}`")
        fix_key = f"{state_key}_fix"
        if isinstance(st.session_state.get(fix_key), dict):
            fix = st.session_state.get(fix_key) or {}
            st.warning("Initial dbt test failed. Review auto-fix below.")
            st.code(str(fix.get("corrected_sql") or ""), language="sql")
            if st.button("Approve Corrected SQL", key=f"approve_fix_{key_prefix}_{table_key}", type="primary"):
                corrected_sql = str(fix.get("corrected_sql") or "").strip()
                if corrected_sql:
                    pending["sql"] = corrected_sql
                    st.session_state[state_key] = pending
                    try:
                        sql_path, schema_path = _write_model_files(
                            output_dir=out_dir,
                            model_name=model_name,
                            sql=corrected_sql,
                            schema_yml=schema_yml,
                        )
                        st.caption(f"Wrote SQL: `{sql_path}`")
                        if str(schema_yml).strip():
                            st.caption(f"Wrote schema: `{schema_path}`")
                    except OSError as exc:
                        st.error(f"Write failed: {exc}")
                        return
                    with st.status(f"Running dbt test on {model_name}...", expanded=True) as status:
                        test_result = migration_agent_tools.test_model(
                            dbt_project_dir=dbt_project_dir,
                            model_name=model_name,
                            target=dbt_target,
                        )
                        if bool(test_result.get("success")):
                            _mark_table_migrated(str(table_key), model_name)
                            st.success(f"✅ {table_key} migrated and tested successfully.")
                        else:
                            st.warning(f"dbt test still failing for {table_key}.")
                            logs = str(test_result.get("logs") or "").strip()
                            if logs:
                                st.code(logs[:4000], language="text")
                        status.update(label="Done", state="complete")
                    st.rerun()
        ac1, ac2 = st.columns(2)
        if ac1.button(
            "✅ Approve & Write",
            key=f"approve_{key_prefix}_{table_key}",
            type="primary",
        ):
            try:
                sql_path, schema_path = _write_model_files(
                    output_dir=out_dir,
                    model_name=model_name,
                    sql=sql,
                    schema_yml=schema_yml,
                )
                st.caption(f"Wrote SQL: `{sql_path}`")
                if str(schema_yml).strip():
                    st.caption(f"Wrote schema: `{schema_path}`")
            except OSError as exc:
                st.error(f"Write failed: {exc}")
                return
            with st.status(f"Running dbt test on {model_name}...", expanded=True) as status:
                test_result = migration_agent_tools.test_model(
                    dbt_project_dir=dbt_project_dir,
                    model_name=model_name,
                    target=dbt_target,
                )
                if bool(test_result.get("success")):
                    _mark_table_migrated(str(table_key), model_name)
                    st.success(f"✅ {table_key} migrated and tested successfully.")
                else:
                    status.write("Running auto-fix...")
                    fix = migration_agent_tools.apply_fix(
                        dbt_project_dir=dbt_project_dir,
                        model_name=model_name,
                        error_log=str(test_result.get("logs") or ""),
                        attempt_history=[],
                    )
                    st.session_state[fix_key] = fix if isinstance(fix, dict) else {"corrected_sql": ""}
                    st.warning(f"dbt test failed for {table_key}. Review suggested fix below.")
                status.update(label="Done", state="complete")
        if ac2.button("❌ Reject", key=f"reject_{key_prefix}_{table_key}"):
            st.session_state.pop(state_key, None)
            st.session_state.pop(fix_key, None)
            st.warning(f"Migration of {table_key} cancelled.")


def _format_sql_for_display(sql: str, *, dialect: str) -> str:
    """
    Pretty-print SQL for UI display only.

    - dbt config/Jinja blocks are stripped before formatting (sqlglot can't parse them)
    - formatted SQL is reattached after the extracted blocks
    """
    import sqlglot

    raw = sql or ""
    if not raw.strip():
        return raw

    blocks = _DBT_JINJA_BLOCK_RE.findall(raw) or []
    # Remove all jinja blocks, keep only the SQL statement body.
    body = _DBT_JINJA_BLOCK_RE.sub("", raw).strip()
    if not body:
        return raw

    # Best-effort dialect handling: if sqlglot can't apply the dialect, fallback to generic formatting.
    formatted_body = body
    try:
        parsed = sqlglot.parse_one(body)
        # sqlglot auto-detects input dialect; we only set an output dialect when possible.
        # pretty=True enforces multi-line formatting for the UI.
        formatted_body = parsed.sql(pretty=True)
    except Exception:
        formatted_body = body

    prefix = "\n".join(b.strip() for b in blocks if isinstance(b, str) and b.strip())
    if prefix:
        return prefix + "\n\n" + formatted_body.strip() + "\n"
    return formatted_body.strip() + "\n"


def _normalize_sql_for_compare(sql: str) -> str:
    """
    Whitespace-insensitive normalization for UI-only formatting changes.

    Used to detect whether the user meaningfully edited SQL (not merely reformatted it).
    """
    if sql is None:
        return ""
    # Collapse all whitespace (spaces/newlines/tabs) into single spaces.
    return re.sub(r"\s+", " ", str(sql).strip())


def _extract_source_relations(sql: str) -> set[str]:
    """
    Best-effort extraction of source relations referenced by FROM/JOIN.

    Used as a lineage safety guard: Fix Agent must not silently rewrite source
    schema/table references unless the user explicitly approves.
    """
    try:
        import sqlglot
        from sqlglot import exp
    except Exception:
        return set()

    raw = str(sql or "")
    body = _DBT_JINJA_BLOCK_RE.sub("", raw).strip()
    if not body:
        return set()
    try:
        root = sqlglot.parse_one(body)
    except Exception:
        return set()

    refs: set[str] = set()
    for tbl in root.find_all(exp.Table):
        name = str(tbl.name or "").strip()
        db = str(tbl.db or "").strip()
        if not name:
            continue
        refs.add(f"{db}.{name}".lower() if db else name.lower())
    return refs


def _extract_top_level_output_columns(sql: str) -> tuple[set[str], bool]:
    """
    Return (explicit_output_columns, has_star_projection) for the top-level SELECT.

    Used as a semantic guard to catch obviously wrong manual edits that remain
    syntactically valid (for example random identifier projections).
    """
    try:
        import sqlglot
        from sqlglot import exp
    except Exception:
        return set(), False

    body = _DBT_JINJA_BLOCK_RE.sub("", str(sql or "")).strip()
    if not body:
        return set(), False
    try:
        root = sqlglot.parse_one(body)
    except Exception:
        return set(), False

    select_node = root if isinstance(root, exp.Select) else root.find(exp.Select)
    if select_node is None:
        return set(), False

    cols: set[str] = set()
    has_star = False
    for proj in select_node.expressions or []:
        if isinstance(proj, exp.Star):
            has_star = True
            continue
        if isinstance(proj, exp.Alias):
            alias = str(proj.alias or "").strip()
            if alias:
                cols.add(alias.lower())
                continue
            base = proj.this
        else:
            base = proj
        if isinstance(base, exp.Column):
            name = str(base.name or "").strip()
            if name:
                cols.add(name.lower())
        elif isinstance(base, exp.Identifier):
            name = str(base.this or "").strip()
            if name:
                cols.add(name.lower())
    return cols, has_star


def _find_suspicious_cast_types(sql: str) -> list[str]:
    """
    Detect CAST targets that look invalid or unintended.

    sqlglot parses many unknown type identifiers as generic DataType tokens, so we
    add a defensive allow-list check to catch typos like `VARCHAds...` early.
    """
    try:
        import sqlglot
        from sqlglot import exp
    except Exception:
        return []

    body = _DBT_JINJA_BLOCK_RE.sub("", str(sql or "")).strip()
    if not body:
        return []
    try:
        root = sqlglot.parse_one(body)
    except Exception:
        return []

    allowed = {
        # common/sqlglot-friendly core
        "CHAR",
        "VARCHAR",
        "TEXT",
        "STRING",
        "NCHAR",
        "NVARCHAR",
        "INT",
        "INTEGER",
        "BIGINT",
        "SMALLINT",
        "TINYINT",
        "HUGEINT",
        "UBIGINT",
        "UINTEGER",
        "USMALLINT",
        "UTINYINT",
        "DECIMAL",
        "NUMERIC",
        "FLOAT",
        "DOUBLE",
        "REAL",
        "BOOLEAN",
        "BOOL",
        "DATE",
        "TIME",
        "TIMESTAMP",
        "TIMESTAMPTZ",
        "DATETIME",
        "INTERVAL",
        "UUID",
        "JSON",
        "BLOB",
        "BYTEA",
        "VARBYTE",
        # bigquery / snowflake / redshift extras
        "INT64",
        "FLOAT64",
        "BIGNUMERIC",
        "NUMBER",
        "GEOGRAPHY",
        "SUPER",
        # complex-ish types
        "ARRAY",
        "MAP",
        "STRUCT",
        "LIST",
    }
    suspicious: list[str] = []
    for cast_node in root.find_all(exp.Cast):
        to_expr = cast_node.args.get("to")
        if to_expr is None:
            continue
        raw_type = str(to_expr.sql() if hasattr(to_expr, "sql") else to_expr).strip()
        base = re.sub(r"\(.*\)$", "", raw_type).strip().upper()
        base = re.sub(r"\s+", " ", base)
        # accept enum-like compound types (e.g., TIMESTAMP WITH TIME ZONE)
        if base in allowed or base.startswith(("ARRAY<", "STRUCT<", "MAP<", "LIST<")):
            continue
        if re.search(r"[A-Z]", base):
            suspicious.append(raw_type)
    return suspicious


def _has_top_level_where_clause(sql: str) -> bool:
    """
    Detect a top-level WHERE clause in the model SELECT.

    In migration mode we preserve full-row semantics by default, so ad-hoc business
    filters should be blocked unless explicitly supported.
    """
    try:
        import sqlglot
        from sqlglot import exp
    except Exception:
        return False

    body = _DBT_JINJA_BLOCK_RE.sub("", str(sql or "")).strip()
    if not body:
        return False
    try:
        root = sqlglot.parse_one(body)
    except Exception:
        return False
    select_node = root if isinstance(root, exp.Select) else root.find(exp.Select)
    if select_node is None:
        return False
    return select_node.args.get("where") is not None

try:
    import plotly.express as px
    import plotly.graph_objects as go
except ImportError:  # pragma: no cover
    px = None  # type: ignore[assignment]
    go = None  # type: ignore[assignment]

# Repo root: src/ama/ui/dashboard.py -> parents[3]
_DEMO_WITH_REVIEW = Path(__file__).resolve().parents[3] / "sample_data" / "dashboard" / "demo_with_review.json"

# No global confidence slider — merge confidence stays visual (scatter, gauges, table columns).
_MERGE_CONF_SCOPE = 0.0


def _render_dq_tab(report: dict[str, Any]) -> None:
    """Data quality suite (same as ``ama-ingest dq``)."""
    st.subheader("Data quality")
    st.caption(
        "Report contract checks: boundary validation, **schema_version**, **ingestion_stats**, and discovery inventory. "
        "Aligns with **`ama-ingest dq --report report.json`**."
    )
    st.caption("Run the suite against this loaded report.")
    if "dq_last" not in st.session_state:
        st.session_state["dq_last"] = None
    if st.button("Run DQ Checks", key="dq_run_btn"):
        st.session_state["dq_last"] = run_dq_suite(report)

    dq = st.session_state["dq_last"] or run_dq_suite(report)
    d = dq.to_dict()
    c1, c2, c3 = st.columns(3)
    c1.metric("Suite status", "PASS" if d["ok"] else "FAIL")
    c2.metric("Errors", int(d["error_count"]))
    c3.metric("Warnings", int(d["warn_count"]))
    chk = d.get("checks") or []
    if chk:
        st.dataframe(pd.DataFrame(chk), use_container_width=True, hide_index=True)
    else:
        st.info("No check rows returned.")


def _render_planner_tab(report: dict[str, Any]) -> None:
    """Migration waves from discovery inventory (same as ``ama-ingest plan``)."""
    st.subheader("Planner")
    st.caption(
        "System-wide waves by domain + priority, with **short** business/technical blurbs from *this* report’s inventory. "
        "Same as **`ama-ingest plan --report report.json`**."
    )
    plan = AutonomousPlanner().plan_from_report(report, max_tables_per_wave=25, max_waves=20)
    plan_dict = plan.to_dict()
    st.markdown(f"**Migration context:** `{plan_dict.get('migration_context') or plan_dict.get('target_focus') or '—'}`")
    notes = plan_dict.get("notes") or []
    if notes:
        for n in notes:
            st.caption(str(n))
    waves = plan_dict.get("waves") or []
    if waves:
        st.markdown("---")
        wave_ids = []
        for w in waves:
            if isinstance(w, dict) and "wave_id" in w:
                wave_ids.append(w["wave_id"])
        wave_ids_num: set[int] = set()
        for x in wave_ids:
            try:
                if isinstance(x, (int, float, str)):
                    wave_ids_num.add(int(x))
            except (TypeError, ValueError):
                continue
        wave_ids_sorted = sorted(wave_ids_num)
        if wave_ids_sorted:
            st.caption(
                "Tip (Migration Agent): try `Migrate Wave <id>`, `Continue Wave <id>`, `Show Status`, or `Skip Current`."
            )
    st.metric("Migration waves", len(waves))
    if not waves:
        st.info(
            "No waves — enable **discovery** and ingest SQL logs with **`--discovery-mode`** so the inventory populates."
        )
        return
    for w in waves:
        if not isinstance(w, dict):
            continue
        wid = w.get("wave_id", "")
        wname = w.get("name", "")
        tbls = w.get("tables") or []
        with st.expander(f"Wave {wid}: {wname} ({len(tbls)} tables)", expanded=False):
            br = str(w.get("business_rationale") or "").strip()
            tr = str(w.get("technical_rationale") or "").strip()
            if br:
                st.markdown("**Business rationale**")
                st.markdown(br)
            if tr:
                st.markdown("**Technical rationale**")
                st.markdown(tr)
            if tbls:
                st.dataframe(pd.DataFrame(tbls), use_container_width=True, hide_index=True)
            else:
                st.caption("No tables in this wave.")


def render_tool_output(tool_name: str, result: Any) -> None:
    """
    Migration Agent Intelligence Feed renderer.

    Tool results must NEVER be rendered as raw JSON.
    """
    if not isinstance(result, dict):
        st.write("✅ Command Executed")
        return

    # Tool execution error payloads
    if "error" in result and isinstance(result.get("error"), str):
        with st.expander(f"View Error (tool: {tool_name})", expanded=False):
            st.error(str(result.get("error")))
        return

    if tool_name == "list_waves":
        waves = result.get("waves") if isinstance(result.get("waves"), list) else []
        rows: list[dict[str, Any]] = []
        for w in waves:
            if not isinstance(w, dict):
                continue
            tables_val = w.get("tables")
            if isinstance(tables_val, list):
                tables_disp = len(tables_val)
            else:
                tables_disp = tables_val if tables_val is not None else "—"
            rows.append(
                {
                    "Wave": w.get("wave_id") if w.get("wave_id") is not None else "—",
                    "Tables": tables_disp,
                    "Status": str(w.get("status") or "PENDING"),
                }
            )
        st.table(rows if rows else [{"Wave": "—", "Tables": "—", "Status": "—"}])
        return

    if tool_name == "analyze_schema":
        ddl_cols = result.get("ddl_columns") if isinstance(result.get("ddl_columns"), list) else []
        observed_cols = result.get("observed_columns") if isinstance(result.get("observed_columns"), list) else []
        inferred_types = result.get("inferred_types") if isinstance(result.get("inferred_types"), dict) else {}
        cols: list[str] = []
        for c in list(ddl_cols) + list(observed_cols):
            if isinstance(c, str) and c.strip():
                cols.append(c.strip())
        cols = list(dict.fromkeys(cols))

        sample_rows = result.get("sample_rows") if isinstance(result.get("sample_rows"), list) else []
        first_row = sample_rows[0] if sample_rows and isinstance(sample_rows[0], dict) else {}
        sample_unavailable = not sample_rows and bool(result.get("sample_rows_warning"))
        rows1: list[dict[str, Any]] = []
        for col in cols[:30]:
            sample_val = first_row.get(col) if isinstance(first_row, dict) else None
            if sample_val is None and sample_unavailable:
                sval = "N/A (source table not available in DuckDB)"
            else:
                sval = "" if sample_val is None else str(sample_val)
            ctype = str(inferred_types.get(col) or "Unknown")
            rows1.append({"Column Name": col, "Type": ctype, "Sample Value": sval})
        st.table(rows1 if rows1 else [{"Column Name": "—", "Type": "—", "Sample Value": ""}])

        hebrew_columns = result.get("hebrew_columns")
        if isinstance(hebrew_columns, list) and hebrew_columns:
            rows2: list[dict[str, Any]] = []
            for r in hebrew_columns:
                if not isinstance(r, dict):
                    continue
                rows2.append(
                    {
                        "Hebrew Name": r.get("hebrew_name") or "—",
                        "English Alias": r.get("english_alias") or "—",
                        "Source": r.get("source") or "—",
                    }
                )
            if rows2:
                st.table(rows2)

        if result.get("sample_rows_warning"):
            with st.expander("Notes", expanded=False):
                warn_kind = str(result.get("sample_rows_warning_kind") or "")
                if warn_kind == "source_table_missing":
                    st.info(
                        "Live source rows are not available in local DuckDB for this table. "
                        "Showing synthetic sample values inferred from DDL."
                    )
                else:
                    st.warning("DuckDB sample-row query failed. Showing fallback sample values.")
                if str(result.get("sample_rows_source") or "") == "synthetic_from_ddl":
                    st.caption("Showing synthetic sample values inferred from DDL columns.")
                with st.expander("Technical details", expanded=False):
                    st.code(str(result.get("sample_rows_warning") or ""), language="text")
        return

    if tool_name == "propose_dbt_model":
        table_key = str(result.get("table_key") or "—")
        model_name = str(result.get("model_name") or "—")
        st.markdown(f"**Proposed dbt model**: `{model_name}`")
        st.caption(f"Source table: `{table_key}`")
        st.caption("SQL Draft")
        sql = str(result.get("sql") or "")
        try:
            mig_state = st.session_state.get("migration_agent") or {}
            dialect = str(mig_state.get("dialect") or "duckdb")
        except Exception:
            dialect = "duckdb"
        st.code(_format_sql_for_display(sql, dialect=dialect), language="sql")

        conf = float(result.get("generation_confidence") or 0.0)
        mode = str(result.get("generation_mode") or "legacy")
        st.caption("Generation Summary")
        st.table([{"Confidence": f"{conf:.2f}", "Generation Mode": mode}])

        mapping_rows = result.get("mapping_rows") if isinstance(result.get("mapping_rows"), list) else []
        if mapping_rows:
            low_cnt = 0
            top_rows: list[dict[str, Any]] = []
            has_hebrew_terms = False
            for rr in mapping_rows:
                if not isinstance(rr, dict):
                    continue
                src_name = str(rr.get("hebrew_name") or "")
                if any("\u0590" <= ch <= "\u05FF" for ch in src_name):
                    has_hebrew_terms = True
                    break
            source_col_label = "Hebrew" if has_hebrew_terms else "Source Column"
            for r in mapping_rows:
                if not isinstance(r, dict):
                    continue
                c = r.get("confidence")
                try:
                    c_val = float(c) if c is not None else None
                except (TypeError, ValueError):
                    c_val = None
                if c_val is not None and c_val < 0.8:
                    low_cnt += 1
                if len(top_rows) < 8:
                    top_rows.append(
                        {
                            source_col_label: r.get("hebrew_name") or "—",
                            "Alias": r.get("english_alias") or "—",
                            "Source": r.get("source") or "—",
                            "Conf": "" if c_val is None else f"{c_val:.2f}",
                        }
                    )
            st.caption(
                f"Mapping summary: showing {min(8, len(mapping_rows))}/{len(mapping_rows)} rows (low conf: {low_cnt})."
            )
            st.table(top_rows)
        return

    if tool_name == "execute_dbt_test":
        model_name = str(result.get("model_name") or result.get("model") or "model")
        success = bool(result.get("success"))
        stage = str(result.get("stage") or "").strip()
        if success:
            st.success(f"✅ **{model_name}** passed dbt run + test")
        else:
            st.error(f"❌ Validation failed{f' at `{stage}`' if stage else ''}")
            logs = str(result.get("logs") or "")
            with st.expander("View Error Log", expanded=False):
                st.code(logs or "-- no logs", language="text")
        run_logs = str(result.get("run_logs") or "")
        if run_logs and not success:
            with st.expander("View dbt run log", expanded=False):
                st.code(run_logs, language="text")
        return

    if tool_name == "apply_fix":
        corrected_sql = str(result.get("corrected_sql") or "")
        try:
            mig_state = st.session_state.get("migration_agent") or {}
            dialect = str(mig_state.get("dialect") or "duckdb")
        except Exception:
            dialect = "duckdb"
        st.code(_format_sql_for_display(corrected_sql, dialect=dialect), language="sql")
        st.write(str(result.get("error_analysis") or ""))

        conf = float(result.get("confidence") or 0.0)
        badge = "🔴"
        if conf >= 0.8:
            badge = "🟢"
        elif conf >= 0.6:
            badge = "🟠"
        st.caption(f"Fix confidence: {badge} {conf:.2f}")
        if bool(result.get("relation_change_blocked")):
            prev_refs = result.get("blocked_prev_relations") if isinstance(result.get("blocked_prev_relations"), list) else []
            new_refs = result.get("blocked_new_relations") if isinstance(result.get("blocked_new_relations"), list) else []
            st.warning("Lineage safety block: Fix SQL changed source relation(s); explicit approval is required.")
            if prev_refs or new_refs:
                st.caption(f"Previous refs: {', '.join(str(x) for x in prev_refs) or '—'}")
                st.caption(f"Fix refs: {', '.join(str(x) for x in new_refs) or '—'}")
        return

    if tool_name == "request_write_permission":
        pending = result.get("pending_write") if isinstance(result.get("pending_write"), dict) else {}
        sql = str(pending.get("sql") or result.get("sql") or "")
        try:
            mig_state = st.session_state.get("migration_agent") or {}
            dialect = str(mig_state.get("dialect") or "duckdb")
        except Exception:
            dialect = "duckdb"
        st.code(_format_sql_for_display(sql, dialect=dialect), language="sql")
        return

    if tool_name == "generate_synthetic_rows":
        table_key = str(result.get("table_key") or "—")
        row_cap = int(result.get("row_cap") or 0)
        source = str(result.get("sample_rows_source") or "unknown")
        rows = result.get("sample_rows") if isinstance(result.get("sample_rows"), list) else []
        st.markdown(f"**Synthetic rows** for `{table_key}`")
        st.caption(f"Requested rows: {row_cap} · Source: `{source or 'unknown'}`")
        if rows:
            df = pd.DataFrame([r for r in rows if isinstance(r, dict)])
            if not df.empty:
                st.dataframe(df.head(10), use_container_width=True, hide_index=True)
                st.success(f"Generated {len(rows)} synthetic row(s).")
            else:
                st.warning("Synthetic payload had no tabular rows.")
        else:
            st.warning("No synthetic rows returned.")
        warn = str(result.get("sample_rows_warning") or "").strip()
        if warn:
            with st.expander("Generation warning", expanded=False):
                st.code(warn, language="text")
        return

    if tool_name == "validate_sql_on_duckdb":
        ok = bool(result.get("ok"))
        dialect = str(result.get("dialect") or "duckdb")
        reasons = result.get("reasons") if isinstance(result.get("reasons"), list) else []
        if ok:
            st.success(f"SQL validation passed ({dialect}).")
        else:
            st.error(f"SQL validation failed ({dialect}).")
            for reason in reasons[:6]:
                st.markdown(f"- {reason}")
        return

    st.success("✅ Command Executed")


def _build_intelligence_feed(messages: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    feed = {
        "waves": [],
        "schema": [],
        "proposals": [],
        "tests": [],
        "fixes": [],
    }
    for msg in messages:
        if not isinstance(msg, dict) or str(msg.get("role") or "") != "tool":
            continue
        tool_name = str(msg.get("tool_name") or "")
        res = msg.get("tool_result")
        if not isinstance(res, dict):
            continue
        if tool_name == "list_waves":
            for w in res.get("waves") or []:
                if isinstance(w, dict):
                    feed["waves"].append(
                        {
                            "Wave": w.get("wave_id"),
                            "Tables": len(w.get("tables") or []),
                            "Status": w.get("status") or "PENDING",
                        }
                    )
        elif tool_name == "analyze_schema":
            feed["schema"].append(
                {
                    "Table": res.get("table_key") or "—",
                    "DDL Columns": len(res.get("ddl_columns") or []),
                    "Observed Columns": len(res.get("observed_columns") or []),
                    "Sample Rows": len(res.get("sample_rows") or []),
                    "Note": "DDL only" if bool(res.get("sample_rows_warning")) else "",
                }
            )
        elif tool_name == "propose_dbt_model":
            feed["proposals"].append(
                {
                    "Table": res.get("table_key") or "—",
                    "Model": res.get("model_name") or "—",
                    "Confidence": f"{float(res.get('generation_confidence') or 0.0):.2f}",
                    "Mode": res.get("generation_mode") or "legacy",
                    "Mappings": len(res.get("mapping_rows") or []),
                }
            )
        elif tool_name == "execute_dbt_test":
            feed["tests"].append(
                {
                    "Model": res.get("model_name") or res.get("model") or "—",
                    "Success": bool(res.get("success")),
                    "Return Code": res.get("return_code"),
                }
            )
        elif tool_name == "apply_fix":
            conf = float(res.get("confidence") or 0.0)
            feed["fixes"].append(
                {
                    "Model": res.get("model_name") or "—",
                    "Confidence": f"{conf:.2f}",
                    "Status": "ready" if bool(res.get("corrected_sql")) else "empty",
                }
            )
    return feed


def _extract_wave_scope_from_messages(messages: list[dict[str, Any]]) -> tuple[int | None, dict[str, str]]:
    """
    Best-effort parse of target wave + table scope from recent user prompt and list_waves result.
    """
    import re

    selected_wave: int | None = None
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        if str(msg.get("role") or "") != "user":
            continue
        text = str(msg.get("content") or "").lower()
        m = re.search(r"\bwave\s+(\d+)\b", text)
        if m:
            try:
                selected_wave = int(m.group(1))
            except ValueError:
                selected_wave = None
            break

    model_to_table: dict[str, str] = {}
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        if str(msg.get("role") or "") != "tool" or str(msg.get("tool_name") or "") != "list_waves":
            continue
        res = msg.get("tool_result")
        if not isinstance(res, dict):
            continue
        waves = res.get("waves")
        if not isinstance(waves, list):
            continue
        for w in waves:
            if not isinstance(w, dict):
                continue
            try:
                wid = int(w.get("wave_id"))
            except (TypeError, ValueError):
                continue
            if selected_wave is not None and wid != selected_wave:
                continue
            for t in w.get("tables") or []:
                if isinstance(t, str) and t.strip():
                    table_key = t.strip()
                    model_to_table[table_key.replace(".", "_")] = table_key
            if selected_wave is not None:
                break
        break
    return selected_wave, model_to_table


def _tool_message_model_name(message: dict[str, Any]) -> str:
    if not isinstance(message, dict):
        return ""
    if str(message.get("role") or "") != "tool":
        return ""
    result = message.get("tool_result")
    if not isinstance(result, dict):
        return ""
    tool_name = str(message.get("tool_name") or "")
    if tool_name == "request_write_permission":
        pending = result.get("pending_write")
        if isinstance(pending, dict):
            return str(pending.get("model_name") or "").strip()
    direct = str(result.get("model_name") or result.get("model") or "").strip()
    if direct:
        return direct
    table_key = str(result.get("table_key") or "").strip()
    if table_key:
        return table_key.replace(".", "_")
    return ""


def _agent_role_for_tool(tool_name: str) -> tuple[str, str]:
    name = str(tool_name or "").strip()
    if name in {"list_waves"}:
        return "Architect", "Planning migration wave sequence."
    if name in {"analyze_schema"}:
        return "Architect", "Analyzing source schema and Hebrew mappings."
    if name in {"propose_dbt_model"}:
        return "Developer", "Drafting dbt SQL and YAML."
    if name in {"execute_dbt_test", "test_model"}:
        return "QA Lead", "Validating generated model with dbt run + test."
    if name in {"apply_fix"}:
        return "Developer", "Applying self-correction based on QA findings."
    if name in {"request_write_permission", "commit_to_disk"}:
        return "QA Lead", "Requesting human approval gate before write."
    return "Agent", f"Running tool: {name or 'unknown'}"


def _render_migration_agent_tab(report: dict[str, Any]) -> None:
    st.subheader("Migration Agent")
    st.session_state.setdefault(
        "migration_agent",
        {
            "messages": [],
            "pending_write": None,
            "tokens_used_total": 0,
            "cost_est_total": 0.0,
            "model_status_by_name": {},
            "dialect": "duckdb",
            "report_path": str(Path(get_env("AMA_REPORT_PATH", "")).resolve()) if get_env("AMA_REPORT_PATH", "").strip() else "",
            "dbt_project_dir": str(Path(".").resolve()),
            "output_dir": str(Path("models/ama_generated").resolve()),
            "glossary_path": "",
            "sample_row_cap": 10,
            "manual_edit_mode": False,
        },
    )
    state: dict[str, Any] = init_migration_agent_state(st.session_state["migration_agent"])

    if not has_openai_api_key():
        st.warning("Set `AMA_OPENAI_API_KEY` (or `OPENAI_API_KEY`) to enable Migration Agent chat.")

    with st.sidebar:
        st.markdown("### Project Configuration")
        st.checkbox(
            "Show full chat history",
            value=False,
            key="migration_agent_show_full_history",
            help="If off, the Agent tab shows only the latest user turn + its tool outputs for clarity.",
        )
        if st.button("Clear Agent Chat", key="migration_agent_clear_chat"):
            state["messages"] = []
            state["pending_write"] = None
            state["model_status_by_name"] = {}
            state["manual_edit_mode"] = False
            st.rerun()

        state["dialect"] = st.selectbox(
            "Deployment Target Dialect",
            options=["duckdb", "snowflake", "bigquery", "redshift"],
            index=["duckdb", "snowflake", "bigquery", "redshift"].index(str(state.get("dialect") or "duckdb")),
            key="migration_agent_dialect",
        )
        st.caption(f"tokens_used: `{int(state.get('tokens_used_total') or 0)}`")
        st.caption(f"cost_est: `${float(state.get('cost_est_total') or 0.0):.4f}`")
        st.divider()
        st.markdown("### Settings")
        state["report_path"] = st.text_input(
            "Report Path",
            value=str(state.get("report_path") or ""),
            placeholder="path/to/report.json",
            key="migration_agent_report_path",
        )
        state["dbt_project_dir"] = st.text_input(
            "dbt Project Dir",
            value=str(state.get("dbt_project_dir") or ""),
            placeholder=".",
            key="migration_agent_dbt_project_dir",
        )
        state["output_dir"] = st.text_input(
            "Output Directory",
            value=str(state.get("output_dir") or ""),
            placeholder="models/ama_generated",
            key="migration_agent_output_dir",
        )
        state["glossary_path"] = st.text_input(
            "Glossary Path (optional)",
            value=str(state.get("glossary_path") or ""),
            placeholder="path/to/glossary.json",
            key="migration_agent_glossary_path",
        )
        state["sample_row_cap"] = int(
            st.number_input(
                "Sample Rows",
                min_value=1,
                max_value=50,
                value=int(state.get("sample_row_cap") or 10),
                step=1,
                key="migration_agent_sample_row_cap",
            )
        )

    report_path = Path(str(state.get("report_path") or "")).expanduser().resolve()
    if not report_path.is_file():
        report_path = Path(str(state.get("report_path") or ".")).expanduser().resolve()
    dbt_project_dir = Path(str(state.get("dbt_project_dir") or ".")).expanduser().resolve()
    output_dir = Path(str(state.get("output_dir") or "models/ama_generated")).expanduser().resolve()
    glossary_path_raw = str(state.get("glossary_path") or "").strip()
    glossary_path = Path(glossary_path_raw).expanduser().resolve() if glossary_path_raw else None

    messages = state.get("messages") or []
    show_full_history = bool(st.session_state.get("migration_agent_show_full_history"))
    if show_full_history:
        display_messages = messages
    else:
        # Only show the latest user turn (+ everything after it). This prevents confusing
        # cross-talk when operators run multiple chats in the same session.
        last_user_idx = None
        for i, m in enumerate(messages):
            if isinstance(m, dict) and str(m.get("role") or "") == "user":
                last_user_idx = i
        if last_user_idx is None:
            display_messages = messages[-60:]
        else:
            display_messages = messages[last_user_idx:]

    visible_messages = [m for m in display_messages if isinstance(m, dict) and str(m.get("role") or "") != "system"]
    pending_write = state.get("pending_write")
    active_model = ""
    if isinstance(pending_write, dict):
        active_model = str(pending_write.get("model_name") or "").strip()
    if not active_model:
        for m in reversed(messages):
            if not isinstance(m, dict):
                continue
            model_guess = _tool_message_model_name(m)
            if model_guess:
                active_model = model_guess
                break
    if visible_messages:
        feed = _build_intelligence_feed(display_messages)
        selected_wave, wave_models = _extract_wave_scope_from_messages(display_messages)
        st.markdown("### Intelligence Feed")
        # Compact collaboration trace for transparency in Agent tab.
        recent_tool_msgs = [m for m in messages if isinstance(m, dict) and str(m.get("role") or "") == "tool"][-12:]
        if recent_tool_msgs:
            with st.status("Collaborations & Reasoning", expanded=False) as status:
                for msg in recent_tool_msgs:
                    role, line = _agent_role_for_tool(str(msg.get("tool_name") or ""))
                    status.write(f"{role}: {line}")
                status.update(label="Latest agent collaboration steps", state="complete")
        # Keep feed compact and decision-oriented (no raw tool-by-tool chatter).
        if feed["proposals"] or feed["tests"] or feed["fixes"] or feed["schema"]:
            model_status: dict[str, dict[str, str]] = {}
            for row in feed["proposals"]:
                m = str(row.get("Model") or "—")
                model_status.setdefault(m, {})
                model_status[m]["Proposed"] = "✅"
                model_status[m]["Confidence"] = str(row.get("Confidence") or "—")
            for row in feed["schema"]:
                t = str(row.get("Table") or "—").replace(".", "_")
                model_status.setdefault(t, {})
                model_status[t]["Schema Analyzed"] = "✅"
            for row in feed["tests"]:
                m = str(row.get("Model") or "—")
                model_status.setdefault(m, {})
                model_status[m]["Test"] = "✅" if bool(row.get("Success")) else "❌"
            for row in feed["fixes"]:
                m = str(row.get("Model") or "—")
                model_status.setdefault(m, {})
                model_status[m]["Fix"] = "✅"

            matrix_rows: list[dict[str, Any]] = []
            ordered_models = sorted(
                model_status.keys(),
                key=lambda x: (0 if active_model and x == active_model else 1, x),
            )
            for model_name in ordered_models:
                item = model_status.get(model_name, {})
                matrix_rows.append(
                    {
                        "Model": model_name,
                        "Schema": item.get("Schema Analyzed", "—"),
                        "Proposed": item.get("Proposed", "—"),
                        "Test": item.get("Test", "—"),
                        "Fix": item.get("Fix", "—"),
                        "Confidence": item.get("Confidence", "—"),
                    }
                )
            st.table(matrix_rows[:30] if matrix_rows else [{"Model": "—", "Schema": "—", "Proposed": "—", "Test": "—", "Fix": "—", "Confidence": "—"}])

            # Wave progress bar (e.g. 1/2) when a wave scope is known.
            if wave_models:
                completed = 0
                for model_name in wave_models.keys():
                    item = model_status.get(model_name, {})
                    if item.get("Test") == "✅":
                        completed += 1
                total = len(wave_models)
                progress = float(completed / max(1, total))
                label = f"{completed}/{total}"
                if selected_wave is not None:
                    st.caption(f"Wave {selected_wave} progress: {label}")
                else:
                    st.caption(f"Wave progress: {label}")
                st.progress(progress)
        elif feed["waves"]:
            # If only waves exist, show one concise waves table.
            st.table(feed["waves"][-10:])
    else:
        st.info(
            "Welcome to Migration Agent. Use wave-focused commands like `Migrate Wave 1`, `Continue Wave 1`, `Show Status`, or `Skip Current`."
        )

    if False and isinstance(pending_write, dict) and pending_write.get("model_name"):
        st.divider()
        model_name = str(pending_write.get("model_name") or "")
        st.info(f"Action required: review and approve SQL for `{model_name}`.")
        st.markdown(f"### Review & Approve: `{model_name}`")
        try:
            mig_state = st.session_state.get("migration_agent") or {}
            dialect = str(mig_state.get("dialect") or "duckdb")
        except Exception:
            dialect = "duckdb"
        st.code(_format_sql_for_display(str(pending_write.get("sql") or ""), dialect=dialect), language="sql")
        if bool(pending_write.get("relation_change_blocked")):
            prev_refs = pending_write.get("blocked_prev_relations") if isinstance(pending_write.get("blocked_prev_relations"), list) else []
            new_refs = pending_write.get("blocked_new_relations") if isinstance(pending_write.get("blocked_new_relations"), list) else []
            st.warning(
                "Lineage safety check: Fix Agent changed source relation(s). "
                "Review and explicitly approve before using corrected SQL."
            )
            if prev_refs or new_refs:
                st.caption(f"Previous refs: {', '.join(str(x) for x in prev_refs) or '—'}")
                st.caption(f"Fix refs: {', '.join(str(x) for x in new_refs) or '—'}")
            if st.button("Approve Relation Change and Use Fix SQL", key=f"approve_relation_change_{model_name}"):
                candidate = str(pending_write.get("blocked_candidate_sql") or "")
                if candidate.strip():
                    pending_write["sql"] = candidate
                    pending_write["relation_change_blocked"] = False
                    pending_write.pop("blocked_candidate_sql", None)
                    pending_write.pop("blocked_prev_relations", None)
                    pending_write.pop("blocked_new_relations", None)
                    state["pending_write"] = pending_write
                    st.success("Relation change approved. Corrected SQL loaded into approval gate.")
                    st.rerun()
        mapping_rows = pending_write.get("mapping_rows")
        if not isinstance(mapping_rows, list) or not mapping_rows:
            # Fallback: reuse mapping rows from the last `propose_dbt_model` tool output.
            for msg in reversed(state.get("messages") or []):
                if not isinstance(msg, dict):
                    continue
                if msg.get("role") != "tool" or str(msg.get("tool_name") or "") != "propose_dbt_model":
                    continue
                tr = msg.get("tool_result")
                if isinstance(tr, dict) and isinstance(tr.get("mapping_rows"), list):
                    mapping_rows = tr.get("mapping_rows")
                    break

        if isinstance(mapping_rows, list) and mapping_rows:
            heb_rows = [
                r
                for r in mapping_rows
                if isinstance(r, dict) and any("\u0590" <= ch <= "\u05FF" for ch in str(r.get("hebrew_name") or ""))
            ]
            if heb_rows:
                rows: list[dict[str, Any]] = []
                for r in heb_rows:
                    rows.append(
                        {
                            "Hebrew Name": r.get("hebrew_name") or "—",
                            "English Alias": r.get("english_alias") or "—",
                            "Source": r.get("source") or "—",
                        }
                    )
                st.table(rows[:30])
        edited_sql = str(pending_write.get("sql") or "")
        if bool(state.get("manual_edit_mode", False)):
            edited_sql = st.text_area(
                "Edit SQL before writing",
                value=_format_sql_for_display(str(pending_write.get("sql") or ""), dialect=str(mig_state.get("dialect") or "duckdb")),
                height=200,
                key=f"migration_agent_edit_sql_{model_name}",
            )
            save_col1, save_col2 = st.columns([1, 3])
            if save_col1.button("Save Edited SQL", key=f"migration_agent_save_edit_{model_name}"):
                pending_write["sql"] = edited_sql
                state["pending_write"] = pending_write
                state["manual_edit_mode"] = False
                st.success("Edited SQL saved.")
                st.rerun()
            save_col2.caption("Use this button to save your edits before approval.")

        btn1, btn2, btn3 = st.columns(3)
        if btn1.button("Approve ✅", key="migration_agent_approve_write"):
            sql_to_write = edited_sql if bool(state.get("manual_edit_mode", False)) else str(pending_write.get("sql") or "")
            schema_yml = str(pending_write.get("schema_yml") or "")
            output_dir.mkdir(parents=True, exist_ok=True)
            sql_path = output_dir / f"{model_name}.sql"
            schema_path = output_dir / f"{model_name}.schema.yml"
            try:
                sql_path.write_text(sql_to_write.rstrip() + "\n", encoding="utf-8")
                if schema_yml.strip():
                    schema_path.write_text(schema_yml, encoding="utf-8")
            except OSError as exc:
                st.error(f"Write failed: {exc}")
                return

            with st.status("Agent is working...", expanded=True) as status:
                status.write(f"Running dbt run + test on {model_name}")
                test_result = migration_agent_tools.test_model(
                    dbt_project_dir=dbt_project_dir,
                    model_name=model_name,
                )
                state["model_status_by_name"][model_name] = "SUCCESS" if bool(test_result.get("success")) else "HITL_REQUIRED"
                state["pending_write"] = None
                state["manual_edit_mode"] = False
                if not bool(test_result.get("success")):
                    status.write(f"Running Fix Agent on {model_name}")
                    fix = migration_agent_tools.apply_fix(
                        dbt_project_dir=dbt_project_dir,
                        model_name=model_name,
                        error_log=str(test_result.get("logs") or ""),
                        attempt_history=[],
                    )
                    corrected_sql = str(fix.get("corrected_sql") or "")
                    fix_payload = dict(fix) if isinstance(fix, dict) else {"corrected_sql": corrected_sql}
                    if corrected_sql.strip():
                        previous_refs = _extract_source_relations(sql_to_write)
                        corrected_refs = _extract_source_relations(corrected_sql)
                        relation_changed = bool(previous_refs and corrected_refs and previous_refs != corrected_refs)
                        if relation_changed:
                            pending = migration_agent_tools.request_write_permission(
                                model=model_name,
                                sql=sql_to_write,
                                mapping_rows=mapping_rows if isinstance(mapping_rows, list) else None,
                            ).get("pending_write") or {}
                            pending["relation_change_blocked"] = True
                            pending["blocked_candidate_sql"] = corrected_sql
                            pending["blocked_prev_relations"] = sorted(previous_refs)
                            pending["blocked_new_relations"] = sorted(corrected_refs)
                            fix_payload["relation_change_blocked"] = True
                            fix_payload["blocked_prev_relations"] = sorted(previous_refs)
                            fix_payload["blocked_new_relations"] = sorted(corrected_refs)
                            fix_payload["error_analysis"] = (
                                str(fix_payload.get("error_analysis") or "").strip()
                                + "\nLineage safety block: corrected SQL changed source relation(s). "
                                "User must explicitly approve relation change before applying."
                            ).strip()
                        else:
                            pending = migration_agent_tools.request_write_permission(
                                model=model_name,
                                sql=corrected_sql,
                                mapping_rows=mapping_rows if isinstance(mapping_rows, list) else None,
                            ).get("pending_write") or {}
                        state["pending_write"] = pending
                    state.setdefault("messages", []).append(
                        {
                            "role": "tool",
                            "tool_name": "apply_fix",
                            "tool_result": fix_payload,
                            "content": json.dumps({"tool_name": "apply_fix", "result": fix_payload}, ensure_ascii=False),
                        }
                    )
                else:
                    # Run cockpit validation stages in Agent flow:
                    # QA Lead risk + scenario analysis before next handoff.
                    status.write(f"QA Lead: Running risk/scenario validation for {model_name}")
                    agent_checkpoint_dir = (dbt_project_dir / "out" / "checkpoints" / "agent_tab").resolve()
                    try:
                        insights_row = analyze_model_risk_and_scenarios(
                            checkpoint_dir=agent_checkpoint_dir,
                            model_name=model_name,
                            sql=sql_to_write,
                        )
                        risk_block = insights_row.get("risk") if isinstance(insights_row, dict) else {}
                        risk_level = str((risk_block or {}).get("risk_level") or "Unknown")
                        scenarios = insights_row.get("scenarios") if isinstance(insights_row, dict) else []
                        state.setdefault("messages", []).append(
                            {
                                "role": "assistant",
                                "content": (
                                    f"QA validation for `{model_name}`: risk=`{risk_level}`, "
                                    f"scenario_checks={len(scenarios) if isinstance(scenarios, list) else 0}."
                                ),
                            }
                        )
                    except Exception as exc:
                        state.setdefault("messages", []).append(
                            {
                                "role": "assistant",
                                "content": f"QA validation stage failed for `{model_name}`: {exc}",
                            }
                        )
                    run_agent_turn(
                        state=state,
                        report=report,
                        report_path=report_path,
                        dbt_project_dir=dbt_project_dir,
                        output_dir=output_dir,
                        glossary_path=glossary_path,
                        tool_result_message={
                            "tool_name": "execute_dbt_test",
                            "result": test_result,
                        },
                        sample_row_cap=int(state.get("sample_row_cap") or 10),
                        on_tool_start=lambda name: status.write(
                            f"{_agent_role_for_tool(name)[0]}: {_agent_role_for_tool(name)[1]}"
                        ),
                        default_dialect=str(state.get("dialect") or "duckdb"),
                    )
                    # Auto-continue wave migration: if this model passed and there are
                    # remaining tables in the selected wave, precompute the next model
                    # and open its write gate without requiring another user prompt.
                    selected_wave, wave_models = _extract_wave_scope_from_messages(state.get("messages") or [])
                    if wave_models:
                        completed_models = {
                            str(k): str(v)
                            for k, v in (state.get("model_status_by_name") or {}).items()
                            if str(v) == "SUCCESS"
                        }
                        remaining = [m for m in sorted(wave_models.keys()) if m not in completed_models]
                        if remaining and not state.get("pending_write"):
                            next_model = remaining[0]
                            next_table = wave_models.get(next_model) or next_model.replace("_", ".", 1)
                            status.write(f"Preparing next table in wave: {next_table}")
                            try:
                                schema_out = migration_agent_tools.analyze_schema(
                                    report=report,
                                    report_path=report_path,
                                    table=next_table,
                                    duckdb_path=dbt_project_dir / "target" / "duckdb.db",
                                    sample_row_cap=int(state.get("sample_row_cap") or 10),
                                )
                                state.setdefault("messages", []).append(
                                    {
                                        "role": "tool",
                                        "tool_name": "analyze_schema",
                                        "tool_result": schema_out,
                                        "content": json.dumps({"tool_name": "analyze_schema", "result": schema_out}, ensure_ascii=False),
                                    }
                                )
                                proposal = migration_agent_tools.propose_dbt_model(
                                    report=report,
                                    report_path=report_path,
                                    table=next_table,
                                    dialect=str(state.get("dialect") or "duckdb"),
                                    glossary_path=glossary_path,
                                )
                                state.setdefault("messages", []).append(
                                    {
                                        "role": "tool",
                                        "tool_name": "propose_dbt_model",
                                        "tool_result": proposal,
                                        "content": json.dumps({"tool_name": "propose_dbt_model", "result": proposal}, ensure_ascii=False),
                                    }
                                )
                                pending = migration_agent_tools.request_write_permission(
                                    model=str(proposal.get("model_name") or next_model),
                                    sql=str(proposal.get("sql") or ""),
                                    schema_yml=str(proposal.get("schema_yml") or ""),
                                    mapping_rows=proposal.get("mapping_rows") if isinstance(proposal.get("mapping_rows"), list) else None,
                                ).get("pending_write") or {}
                                state["pending_write"] = pending
                                state.setdefault("messages", []).append(
                                    {
                                        "role": "assistant",
                                        "content": f"Prepared next table in Wave {selected_wave or ''}: {next_table}. Review and approve when ready.",
                                    }
                                )
                            except Exception as exc:
                                state.setdefault("messages", []).append(
                                    {
                                        "role": "assistant",
                                        "content": f"Auto-continue failed for next table ({next_table}): {exc}",
                                    }
                                )
                status.update(label="Done", state="complete")
            st.rerun()

        if btn2.button("Manual Edit ✏️", key="migration_agent_manual_edit"):
            state["manual_edit_mode"] = True
            st.rerun()
        if btn3.button("Skip ⏭️", key="migration_agent_cancel_write"):
            state["pending_write"] = None
            state["manual_edit_mode"] = False
            with st.status("Agent is working...", expanded=False) as status:
                run_agent_turn(
                    state=state,
                    report=report,
                    report_path=report_path,
                    dbt_project_dir=dbt_project_dir,
                    output_dir=output_dir,
                    glossary_path=glossary_path,
                    tool_result_message={
                        "tool_name": "request_write_permission",
                        "approved": False,
                        "message": "User cancelled write.",
                    },
                    sample_row_cap=int(state.get("sample_row_cap") or 10),
                    on_tool_start=lambda name: status.write(
                        f"{_agent_role_for_tool(name)[0]}: {_agent_role_for_tool(name)[1]}"
                    ),
                    default_dialect=str(state.get("dialect") or "duckdb"),
                )
                status.update(label="Done", state="complete")
            st.rerun()

    if visible_messages:
        st.divider()
        # Explicit robust chat bubble rendering.
        hidden_tool_count = 0
        hidden_tools: list[dict[str, Any]] = []
        for message in display_messages:
            if not isinstance(message, dict):
                continue
            role = str(message.get("role") or "")
            if role == "system":
                continue
            if role == "tool":
                msg_model = _tool_message_model_name(message)
                tool_name = str(message.get("tool_name") or "")
                if tool_name == "request_write_permission":
                    # SQL is already shown in the dedicated approval gate.
                    continue
                if tool_name == "list_waves" and selected_wave is not None:
                    # When user targeted a specific wave, hide global waves list noise.
                    continue
                if active_model and msg_model and msg_model != active_model:
                    hidden_tool_count += 1
                    hidden_tools.append(message)
                    continue
            chat_role = role if role in {"assistant", "user"} else "assistant"
            avatar = "🤖" if chat_role == "assistant" else "👤"
            with st.chat_message(chat_role, avatar=avatar):
                if role == "tool":
                    st.markdown(f"🔧 `{str(message.get('tool_name') or 'tool')}`")
                    st.caption("Tool result")
                else:
                    st.markdown(str(message.get("content") or ""))
                tool_name = str(message.get("tool_name") or "")
                tool_payload = message.get("tool_results")
                if tool_payload is None:
                    tool_payload = message.get("tool_result")
                if tool_name and tool_payload is not None:
                    render_tool_output(tool_name, tool_payload)
        if hidden_tool_count > 0:
            with st.expander(f"Previous table details ({hidden_tool_count})", expanded=False):
                for message in hidden_tools:
                    tool_name = str(message.get("tool_name") or "tool")
                    st.markdown(f"🔧 `{tool_name}`")
                    tool_payload = message.get("tool_result")
                    if isinstance(tool_payload, dict):
                        render_tool_output(tool_name, tool_payload)

    # Keep approval gate anchored at the bottom, just above chat input.
    if isinstance(pending_write, dict) and pending_write.get("model_name"):
        st.divider()
        model_name = str(pending_write.get("model_name") or "")
        st.info(f"Action required: review and approve SQL for `{model_name}`.")
        st.markdown(f"### Review & Approve: `{model_name}`")
        try:
            mig_state = st.session_state.get("migration_agent") or {}
            dialect = str(mig_state.get("dialect") or "duckdb")
        except Exception:
            dialect = "duckdb"
        st.code(_format_sql_for_display(str(pending_write.get("sql") or ""), dialect=dialect), language="sql")
        if bool(pending_write.get("relation_change_blocked")):
            prev_refs = pending_write.get("blocked_prev_relations") if isinstance(pending_write.get("blocked_prev_relations"), list) else []
            new_refs = pending_write.get("blocked_new_relations") if isinstance(pending_write.get("blocked_new_relations"), list) else []
            st.warning(
                "Lineage safety check: Fix Agent changed source relation(s). "
                "Review and explicitly approve before using corrected SQL."
            )
            if prev_refs or new_refs:
                st.caption(f"Previous refs: {', '.join(str(x) for x in prev_refs) or '—'}")
                st.caption(f"Fix refs: {', '.join(str(x) for x in new_refs) or '—'}")
            if st.button("Approve Relation Change and Use Fix SQL", key=f"approve_relation_change_bottom_{model_name}"):
                candidate = str(pending_write.get("blocked_candidate_sql") or "")
                if candidate.strip():
                    pending_write["sql"] = candidate
                    pending_write["relation_change_blocked"] = False
                    pending_write.pop("blocked_candidate_sql", None)
                    pending_write.pop("blocked_prev_relations", None)
                    pending_write.pop("blocked_new_relations", None)
                    state["pending_write"] = pending_write
                    st.success("Relation change approved. Corrected SQL loaded into approval gate.")
                    st.rerun()

        mapping_rows = pending_write.get("mapping_rows")
        if not isinstance(mapping_rows, list) or not mapping_rows:
            for msg in reversed(state.get("messages") or []):
                if not isinstance(msg, dict):
                    continue
                if msg.get("role") != "tool" or str(msg.get("tool_name") or "") != "propose_dbt_model":
                    continue
                tr = msg.get("tool_result")
                if isinstance(tr, dict) and isinstance(tr.get("mapping_rows"), list):
                    mapping_rows = tr.get("mapping_rows")
                    break

        edited_sql = str(pending_write.get("sql") or "")
        if bool(state.get("manual_edit_mode", False)):
            edited_sql = st.text_area(
                "Edit SQL before writing",
                value=_format_sql_for_display(
                    str(pending_write.get("sql") or ""),
                    dialect=dialect if isinstance(dialect, str) and dialect else "duckdb",
                ),
                height=200,
                key=f"migration_agent_edit_sql_{model_name}",
            )

        btn1, btn2, btn3 = st.columns(3)
        if btn1.button("Approve ✅", key="migration_agent_approve_write_bottom"):
            original_sql = str(pending_write.get("sql") or "")
            used_manual_edit = bool(state.get("manual_edit_mode", False))
            sql_to_write = edited_sql if used_manual_edit else original_sql
            manual_fix_applied = used_manual_edit and _normalize_sql_for_compare(sql_to_write) != _normalize_sql_for_compare(original_sql)
            schema_yml = str(pending_write.get("schema_yml") or "")
            # Lineage guard for manual edits: source relations should not change silently.
            # (Fix-Agent relation changes are handled via explicit approval flow separately.)
            if used_manual_edit:
                original_refs = _extract_source_relations(original_sql)
                edited_refs = _extract_source_relations(sql_to_write)
                if original_refs and edited_refs and original_refs != edited_refs:
                    st.error(
                        "QA Lead rejected SQL: manual edit changed source relation(s). "
                        "For migration safety, keep the original source lineage."
                    )
                    st.caption(f"Original refs: {', '.join(sorted(original_refs)[:8])}")
                    st.caption(f"Edited refs: {', '.join(sorted(edited_refs)[:8])}")
                    state["manual_edit_mode"] = True
                    return

            # QA Lead gate (syntax only): validate with sqlglot before writing and before dbt test.
            # This makes manual SQL edits immediately visible as "wrong" when they are not parseable.
            try:
                from ama.dbt_migration.sql_self_heal import validate_sql_with_sqlglot
                from ama.dbt_migration.sql_transpile import validate_target_dialect

                target_dialect = validate_target_dialect(str(state.get("dialect") or "duckdb"))
                ok, reasons = validate_sql_with_sqlglot(sql_to_write, target_dialect=target_dialect)
                if not ok:
                    reason_txt = "\n".join(f"- {r}" for r in reasons[:8])
                    st.error(f"QA Lead rejected SQL (sqlglot parse failed):\n{reason_txt}")
                    state["manual_edit_mode"] = True
                    return
            except Exception as exc:
                # Don't block the user if QA validator itself errors; dbt test will still surface compilation errors.
                st.warning(f"QA Lead sqlglot validation skipped due to internal error: {exc}")
            suspicious_casts = _find_suspicious_cast_types(sql_to_write)
            if suspicious_casts:
                st.error(
                    "QA Lead rejected SQL: suspicious CAST target type(s) detected. "
                    "Please fix cast type names before approval."
                )
                st.caption("Suspicious cast types: " + ", ".join(sorted(set(suspicious_casts))[:8]))
                state["manual_edit_mode"] = True
                return
            # Migration guardrail: block business row filters in model SQL by default.
            # Exception: keep incremental predicate patterns used by dbt incremental models.
            if "is_incremental()" not in sql_to_write and _has_top_level_where_clause(sql_to_write):
                st.error(
                    "QA Lead rejected SQL: row-level WHERE filter detected. "
                    "Migration models must preserve all source rows by default."
                )
                st.caption("Remove business filters (for example date/status predicates) from migration models.")
                state["manual_edit_mode"] = True
                return
            # Semantic guard: block random/suspicious manual projections that no longer
            # align with mapped target columns (while still being syntactically valid SQL).
            if isinstance(mapping_rows, list) and mapping_rows:
                expected_aliases = {
                    str(r.get("english_alias") or "").strip().lower()
                    for r in mapping_rows
                    if isinstance(r, dict) and str(r.get("english_alias") or "").strip()
                }
                projected_cols, has_star = _extract_top_level_output_columns(sql_to_write)
                if expected_aliases and projected_cols and not has_star and projected_cols.isdisjoint(expected_aliases):
                    st.error(
                        "QA Lead rejected SQL: projected columns do not match expected model aliases. "
                        "This looks like a semantic mismatch (not a migration-safe projection)."
                    )
                    st.caption(f"Expected aliases (sample): {', '.join(sorted(expected_aliases)[:8])}")
                    st.caption(f"Projected columns: {', '.join(sorted(projected_cols)[:8])}")
                    state["manual_edit_mode"] = True
                    return

            output_dir.mkdir(parents=True, exist_ok=True)
            sql_path = output_dir / f"{model_name}.sql"
            schema_path = output_dir / f"{model_name}.schema.yml"
            try:
                sql_path.write_text(sql_to_write.rstrip() + "\n", encoding="utf-8")
                if schema_yml.strip():
                    schema_path.write_text(schema_yml, encoding="utf-8")
            except OSError as exc:
                st.error(f"Write failed: {exc}")
                return

            with st.status("Agent is working...", expanded=True) as status:
                status.write(f"Running dbt run + test on {model_name}")
                test_result = migration_agent_tools.test_model(
                    dbt_project_dir=dbt_project_dir,
                    model_name=model_name,
                )
                state["model_status_by_name"][model_name] = "SUCCESS" if bool(test_result.get("success")) else "HITL_REQUIRED"
                state["pending_write"] = None
                state["manual_edit_mode"] = False
                if not bool(test_result.get("success")):
                    status.write(f"Running Fix Agent on {model_name}")
                    fix = migration_agent_tools.apply_fix(
                        dbt_project_dir=dbt_project_dir,
                        model_name=model_name,
                        error_log=str(test_result.get("logs") or ""),
                        attempt_history=[],
                    )
                    corrected_sql = str(fix.get("corrected_sql") or "")
                    fix_payload = dict(fix) if isinstance(fix, dict) else {"corrected_sql": corrected_sql}
                    if corrected_sql.strip():
                        previous_refs = _extract_source_relations(sql_to_write)
                        corrected_refs = _extract_source_relations(corrected_sql)
                        relation_changed = bool(previous_refs and corrected_refs and previous_refs != corrected_refs)
                        if relation_changed:
                            pending = migration_agent_tools.request_write_permission(
                                model=model_name,
                                sql=sql_to_write,
                                mapping_rows=mapping_rows if isinstance(mapping_rows, list) else None,
                            ).get("pending_write") or {}
                            pending["relation_change_blocked"] = True
                            pending["blocked_candidate_sql"] = corrected_sql
                            pending["blocked_prev_relations"] = sorted(previous_refs)
                            pending["blocked_new_relations"] = sorted(corrected_refs)
                            fix_payload["relation_change_blocked"] = True
                            fix_payload["blocked_prev_relations"] = sorted(previous_refs)
                            fix_payload["blocked_new_relations"] = sorted(corrected_refs)
                            fix_payload["error_analysis"] = (
                                str(fix_payload.get("error_analysis") or "").strip()
                                + "\nLineage safety block: corrected SQL changed source relation(s). "
                                "User must explicitly approve relation change before applying."
                            ).strip()
                        else:
                            pending = migration_agent_tools.request_write_permission(
                                model=model_name,
                                sql=corrected_sql,
                                mapping_rows=mapping_rows if isinstance(mapping_rows, list) else None,
                            ).get("pending_write") or {}
                        state["pending_write"] = pending
                    state.setdefault("messages", []).append(
                        {
                            "role": "tool",
                            "tool_name": "apply_fix",
                            "tool_result": fix_payload,
                            "content": json.dumps({"tool_name": "apply_fix", "result": fix_payload}, ensure_ascii=False),
                        }
                    )
                else:
                    if manual_fix_applied:
                        manual_fix_result = {
                            "model_name": model_name,
                            "corrected_sql": sql_to_write,
                            "error_analysis": "Manual SQL edit approved and applied by user.",
                            "confidence": 1.0,
                            "source": "manual_edit",
                        }
                        state.setdefault("messages", []).append(
                            {
                                "role": "tool",
                                "tool_name": "apply_fix",
                                "tool_result": manual_fix_result,
                                "content": json.dumps({"tool_name": "apply_fix", "result": manual_fix_result}, ensure_ascii=False),
                            }
                        )
                    run_agent_turn(
                        state=state,
                        report=report,
                        report_path=report_path,
                        dbt_project_dir=dbt_project_dir,
                        output_dir=output_dir,
                        glossary_path=glossary_path,
                        tool_result_message={"tool_name": "execute_dbt_test", "result": test_result},
                        sample_row_cap=int(state.get("sample_row_cap") or 10),
                        on_tool_start=lambda name: status.write(
                            f"{_agent_role_for_tool(name)[0]}: {_agent_role_for_tool(name)[1]}"
                        ),
                        default_dialect=str(state.get("dialect") or "duckdb"),
                    )
                    # Auto-continue wave migration: prepare next table after success.
                    selected_wave, wave_models = _extract_wave_scope_from_messages(state.get("messages") or [])
                    if wave_models:
                        completed_models = {
                            str(k): str(v)
                            for k, v in (state.get("model_status_by_name") or {}).items()
                            if str(v) == "SUCCESS"
                        }
                        remaining = [m for m in sorted(wave_models.keys()) if m not in completed_models]
                        if remaining and not state.get("pending_write"):
                            next_model = remaining[0]
                            next_table = wave_models.get(next_model) or next_model.replace("_", ".", 1)
                            status.write(f"Preparing next table in wave: {next_table}")
                            try:
                                schema_out = migration_agent_tools.analyze_schema(
                                    report=report,
                                    report_path=report_path,
                                    table=next_table,
                                    duckdb_path=dbt_project_dir / "target" / "duckdb.db",
                                    sample_row_cap=int(state.get("sample_row_cap") or 10),
                                )
                                state.setdefault("messages", []).append(
                                    {
                                        "role": "tool",
                                        "tool_name": "analyze_schema",
                                        "tool_result": schema_out,
                                        "content": json.dumps({"tool_name": "analyze_schema", "result": schema_out}, ensure_ascii=False),
                                    }
                                )
                                proposal = migration_agent_tools.propose_dbt_model(
                                    report=report,
                                    report_path=report_path,
                                    table=next_table,
                                    dialect=str(state.get("dialect") or "duckdb"),
                                    glossary_path=glossary_path,
                                )
                                state.setdefault("messages", []).append(
                                    {
                                        "role": "tool",
                                        "tool_name": "propose_dbt_model",
                                        "tool_result": proposal,
                                        "content": json.dumps({"tool_name": "propose_dbt_model", "result": proposal}, ensure_ascii=False),
                                    }
                                )
                                pending = migration_agent_tools.request_write_permission(
                                    model=str(proposal.get("model_name") or next_model),
                                    sql=str(proposal.get("sql") or ""),
                                    schema_yml=str(proposal.get("schema_yml") or ""),
                                    mapping_rows=proposal.get("mapping_rows") if isinstance(proposal.get("mapping_rows"), list) else None,
                                ).get("pending_write") or {}
                                state["pending_write"] = pending
                                state.setdefault("messages", []).append(
                                    {
                                        "role": "assistant",
                                        "content": f"Prepared next table in Wave {selected_wave or ''}: {next_table}. Review and approve when ready.",
                                    }
                                )
                            except Exception as exc:
                                state.setdefault("messages", []).append(
                                    {
                                        "role": "assistant",
                                        "content": f"Auto-continue failed for next table ({next_table}): {exc}",
                                    }
                                )
                status.update(label="Done", state="complete")
            st.rerun()
        if btn2.button("Manual Edit ✏️", key="migration_agent_manual_edit_bottom"):
            state["manual_edit_mode"] = True
            st.rerun()
        if btn3.button("Skip ⏭️", key="migration_agent_cancel_write_bottom"):
            state["pending_write"] = None
            state["manual_edit_mode"] = False
            with st.status("Agent is working...", expanded=False) as status:
                run_agent_turn(
                    state=state,
                    report=report,
                    report_path=report_path,
                    dbt_project_dir=dbt_project_dir,
                    output_dir=output_dir,
                    glossary_path=glossary_path,
                    tool_result_message={"tool_name": "request_write_permission", "approved": False, "message": "User cancelled write."},
                    sample_row_cap=int(state.get("sample_row_cap") or 10),
                    on_tool_start=lambda name: status.write(
                        f"{_agent_role_for_tool(name)[0]}: {_agent_role_for_tool(name)[1]}"
                    ),
                    default_dialect=str(state.get("dialect") or "duckdb"),
                )
                status.update(label="Done", state="complete")
            st.rerun()

    if AGENT_PREFILL_KEY in st.session_state and str(st.session_state.get(AGENT_PREFILL_KEY) or "").strip():
        if not str(st.session_state.get("migration_agent_chat_input") or "").strip():
            st.session_state["migration_agent_chat_input"] = str(st.session_state.get(AGENT_PREFILL_KEY) or "")
            st.session_state[AGENT_PREFILL_KEY] = ""
    user_prompt = st.chat_input(
        "Try: Migrate Wave 1 (or: Show Status, Skip Current)",
        key="migration_agent_chat_input",
    )
    if user_prompt:
        with st.status("Agent is working...", expanded=True) as status:
            status.write("Planning next migration actions")
            run_agent_turn(
                state=state,
                report=report,
                report_path=report_path,
                dbt_project_dir=dbt_project_dir,
                output_dir=output_dir,
                glossary_path=glossary_path,
                user_message=str(user_prompt),
                sample_row_cap=int(state.get("sample_row_cap") or 10),
                on_tool_start=lambda name: status.write(
                    f"{_agent_role_for_tool(name)[0]}: {_agent_role_for_tool(name)[1]}"
                ),
                default_dialect=str(state.get("dialect") or "duckdb"),
            )
            # Pre-approval QA visibility: run risk/scenario validation right after proposal
            # so users can see all role stages (Architect + Developer + QA Lead) in one cycle.
            pending_after_turn = state.get("pending_write")
            if isinstance(pending_after_turn, dict) and pending_after_turn.get("model_name"):
                qa_model = str(pending_after_turn.get("model_name") or "").strip()
                qa_sql = str(pending_after_turn.get("sql") or "")
                if qa_model and qa_sql.strip():
                    status.write(f"QA Lead: Running risk/scenario validation for {qa_model}")
                    agent_checkpoint_dir = (dbt_project_dir / "out" / "checkpoints" / "agent_tab").resolve()
                    try:
                        insights_row = analyze_model_risk_and_scenarios(
                            checkpoint_dir=agent_checkpoint_dir,
                            model_name=qa_model,
                            sql=qa_sql,
                        )
                        risk_block = insights_row.get("risk") if isinstance(insights_row, dict) else {}
                        risk_level = str((risk_block or {}).get("risk_level") or "Unknown")
                        scenarios = insights_row.get("scenarios") if isinstance(insights_row, dict) else []
                        state.setdefault("messages", []).append(
                            {
                                "role": "assistant",
                                "content": (
                                    f"QA validation for `{qa_model}` (pre-approval): risk=`{risk_level}`, "
                                    f"scenario_checks={len(scenarios) if isinstance(scenarios, list) else 0}."
                                ),
                            }
                        )
                    except Exception as exc:
                        state.setdefault("messages", []).append(
                            {
                                "role": "assistant",
                                "content": f"QA validation stage failed for `{qa_model}` (pre-approval): {exc}",
                            }
                        )
            status.update(label="Done", state="complete")
        st.rerun()


def _render_dbt_migration_tab(report: dict[str, Any]) -> None:
    """Interactive wrapper for generating and executing dbt models from an AMA report."""
    st.subheader("dbt Migration")
    st.caption(
        "Zero-terminal flow: generate Checkpoint A, inline edit, approve waves, then resolve Checkpoint B and DLQ."
    )
    pricing_map = {
        "gpt-4o-mini": {"input_per_1k": 0.00015, "output_per_1k": 0.0006},
        "default": {"input_per_1k": 0.0002, "output_per_1k": 0.0008},
    }

    def _badge_for_model(model_art: Any, user_modified: bool) -> str:
        if user_modified:
            return "🛠️ [User Modified]"
        mode = str(getattr(model_art, "generation_mode", "legacy")).lower()
        if mode == "ai":
            return "🟢 [🤖 AI]"
        return "⚙️ [Legacy]"

    def _confidence_style(conf: float) -> str:
        if conf < 0.6:
            return "🔴"
        if conf < 0.8:
            return "🟠"
        return "🟢"

    def _estimate_cost(total_tokens: int) -> float:
        model_name = str(get_openai_model("default"))
        rates = pricing_map.get(model_name, pricing_map["default"])
        # Approx split since telemetry stores total tokens only.
        return ((total_tokens / 2) / 1000.0) * rates["input_per_1k"] + ((total_tokens / 2) / 1000.0) * rates["output_per_1k"]

    # Session init
    st.session_state.setdefault(
        "dbt_migration",
        {
            "target_dialect": "duckdb",
            "glossary_path": "",
            "report_path": "",
            "output_dir": str(Path("models/ama_generated").resolve()),
            "dbt_project_dir": str(Path(".").resolve()),
            "checkpoint_dir": str(Path("out/checkpoints").resolve()),
            "dlq_dir": str(Path("out/dbt_dlq").resolve()),
            "checkpoint_a": None,
            "generate_job_id": None,
            "events_job_id": "",
            "events_lines_seen": 0,
            "wave_plan": {},
            "wave_status": {},
            "model_status": {},
            "edited_sql": {},
            "selected_wave_id": None,
            "selected_model_name": "",
            "wave_exec_job_id": None,
            "wave_exec_job_wave_id": None,
            "auto_refresh_jobs": True,
            "beginner_mode": False,
            "auto_started_checkpoint_a_wave": None,
            "task": None,
            "task_error": "",
            "running_models": [],
            "user_modified": {},
        },
    )
    state: dict[str, Any] = st.session_state["dbt_migration"]

    disc = report.get("discovery") or {}
    migration_context = str(report.get("migration_context") or report.get("target_table") or "").strip()
    if migration_context:
        st.caption(f"Migration context: `{migration_context}`")

    # Resolve report path if available from sidebar
    report_path_resolved: Path | None = None
    default_report_path = get_env("AMA_REPORT_PATH", "").strip()
    if default_report_path and Path(default_report_path).is_file():
        report_path_resolved = Path(default_report_path).resolve()
    # NOTE: When user uploads JSON, we have no stable path; in that case, disable file-based execution.
    if report_path_resolved is None and not state.get("report_path"):
        st.warning("To execute writes, set `AMA_REPORT_PATH` in your environment or provide a local report path.")

    with st.expander("Configuration", expanded=True):
        state["target_dialect"] = st.selectbox(
            "Target Dialect",
            options=["duckdb", "snowflake", "bigquery", "redshift"],
            index=["duckdb", "snowflake", "bigquery", "redshift"].index(str(state.get("target_dialect") or "duckdb")),
        )
        if report_path_resolved is not None:
            state["report_path"] = str(report_path_resolved)
        state["report_path"] = st.text_input(
            "Report Path",
            value=str(state.get("report_path") or ""),
            placeholder="path/to/report.json",
        )
        state["glossary_path"] = st.text_input(
            "Glossary Path (optional)",
            value=str(state.get("glossary_path") or ""),
            placeholder="path/to/glossary.json",
        )
        state["output_dir"] = st.text_input(
            "Output Directory",
            value=str(state.get("output_dir") or ""),
            placeholder="models/ama_generated",
        )
        state["checkpoint_dir"] = st.text_input(
            "Checkpoint B Directory",
            value=str(state.get("checkpoint_dir") or ""),
            placeholder="out/checkpoints",
        )
        state["dlq_dir"] = st.text_input(
            "DLQ Directory",
            value=str(state.get("dlq_dir") or ""),
            placeholder="out/dbt_dlq",
        )
        state["dbt_project_dir"] = st.text_input(
            "dbt Project Dir",
            value=str(state.get("dbt_project_dir") or "."),
            placeholder=".",
        )
        state["beginner_mode"] = bool(
            st.checkbox(
                "Beginner Mode (simplified UI)",
                value=bool(state.get("beginner_mode", False)),
                key="dbt_mig_beginner_mode",
            )
        )
        state["auto_refresh_jobs"] = bool(
            st.checkbox(
                "Auto refresh while jobs run",
                value=bool(state.get("auto_refresh_jobs", True)),
                key="dbt_mig_auto_refresh_jobs",
            )
        )
        bypass_val_default = int(state.get("bypass_wave_id") or 0)
        bypass_val = st.number_input(
            "Bypass Wave ID (optional)",
            min_value=0,
            step=1,
            value=bypass_val_default,
            help="If set (non-zero), the orchestrator skips the integrity gate for this wave and continues to the next wave.",
        )
        # Treat 0 as unset.
        state["bypass_wave_id"] = None if int(bypass_val) == 0 else int(bypass_val)

        # Generate Checkpoint A (no writes unless Checkpoint A approved; this UI uses wave approvals for writes)
        generate_clicked = st.button("Run generate-dbt (Checkpoint A only)", key="dbt_mig_generate")
        if generate_clicked:
            # Clear previous execution state
            state["checkpoint_a"] = None
            state["generate_job_id"] = None
            state["wave_plan"] = {}
            state["wave_status"] = {}
            state["model_status"] = {}
            state["edited_sql"] = {}
            state["running_models"] = []
            state["task"] = None
            state["task_error"] = ""
            state["auto_started_checkpoint_a_wave"] = None

            rp = Path(str(state.get("report_path") or "")).expanduser().resolve()
            if not rp.is_file():
                st.error(f"Report not found: {rp}")
                return
            glossary_p = state.get("glossary_path") or ""
            glossary_path = Path(glossary_p).expanduser().resolve() if glossary_p else None

            models_dir = Path(str(state.get("output_dir") or "")).expanduser().resolve()
            checkpoint_dir = Path(str(state.get("checkpoint_dir") or "")).expanduser().resolve()
            dlq_dir = Path(str(state.get("dlq_dir") or "")).expanduser().resolve()
            dbt_project_dir = Path(str(state.get("dbt_project_dir") or "")).expanduser().resolve()

            job_id, _job_payload = start_generate_dbt_checkpoint_a_job(
                report_path=rp,
                glossary_path=glossary_path,
                target_dialect_raw=str(state["target_dialect"]),
                dbt_models_dir=models_dir,
                dbt_project_dir=dbt_project_dir,
                checkpoint_dir=checkpoint_dir,
                dlq_dir=dlq_dir,
                bypass_wave=state.get("bypass_wave_id"),
                wave_id_filter=(
                    int(state["selected_wave_id"]) if state.get("selected_wave_id") is not None else None
                ),
                stop_on_first_error=False,
                approve_checkpoint_a=False,
                run_execution=False,
            )
            state["generate_job_id"] = job_id
            state["events_job_id"] = job_id
            state["events_lines_seen"] = 0
            wave_hint = (
                f" (Wave {state['selected_wave_id']} only)" if state.get("selected_wave_id") is not None else ""
            )
            st.success(f"Checkpoint A generation started (job_id={job_id}){wave_hint}.")
            if state.get("selected_wave_id") is not None:
                st.caption("Selective generation is active: only the queued wave will be drafted in Checkpoint A.")

        # If the generation logic changes, the mapping preview depends on the persisted
        # `checkpoint_a` artifact. Provide a beginner-friendly “regenerate” button so
        # the user can refresh the draft without hunting for internal state.
        if state.get("selected_wave_id") is not None:
            if st.button("Regenerate Draft (queued wave)", key="dbt_mig_regen_queued"):
                state["checkpoint_a"] = None
                state["generate_job_id"] = None
                state["wave_plan"] = {}
                state["wave_status"] = {}
                state["model_status"] = {}
                state["edited_sql"] = {}
                state["running_models"] = []
                state["task"] = None
                state["task_error"] = ""
                state["auto_started_checkpoint_a_wave"] = None
                st.rerun()

        # Beginner/operator automation:
        # If Planner queued a specific wave and we have no draft yet, auto-start generation once.
        queued_wave_id = state.get("selected_wave_id")
        if queued_wave_id is not None:
            try:
                queued_wave_id_int = int(queued_wave_id)
            except (TypeError, ValueError):
                queued_wave_id_int = None

            if queued_wave_id_int is not None:
                if state.get("checkpoint_a") is None and state.get("generate_job_id") is None:
                    if state.get("auto_started_checkpoint_a_wave") != queued_wave_id_int:
                        rp = Path(str(state.get("report_path") or "")).expanduser().resolve()
                        if not rp.is_file():
                            st.warning("Queued wave detected, but Report Path is missing/invalid.")
                        else:
                            glossary_p = state.get("glossary_path") or ""
                            glossary_path = (
                                Path(glossary_p).expanduser().resolve() if glossary_p else None
                            )
                            models_dir = Path(str(state.get("output_dir") or "")).expanduser().resolve()
                            checkpoint_dir = Path(str(state.get("checkpoint_dir") or "")).expanduser().resolve()
                            dlq_dir = Path(str(state.get("dlq_dir") or "")).expanduser().resolve()
                            dbt_project_dir = Path(str(state.get("dbt_project_dir") or "")).expanduser().resolve()

                            job_id, _job_payload = start_generate_dbt_checkpoint_a_job(
                                report_path=rp,
                                glossary_path=glossary_path,
                                target_dialect_raw=str(state["target_dialect"]),
                                dbt_models_dir=models_dir,
                                dbt_project_dir=dbt_project_dir,
                                checkpoint_dir=checkpoint_dir,
                                dlq_dir=dlq_dir,
                                bypass_wave=state.get("bypass_wave_id"),
                                wave_id_filter=queued_wave_id_int,
                                stop_on_first_error=False,
                                approve_checkpoint_a=False,
                                run_execution=False,
                            )
                            state["generate_job_id"] = job_id
                            state["events_job_id"] = job_id
                            state["events_lines_seen"] = 0
                            state["auto_started_checkpoint_a_wave"] = queued_wave_id_int
                            st.info(
                                f"Auto-started Selective Checkpoint A generation for Wave {queued_wave_id_int} "
                                f"(job_id={job_id})."
                            )
                            if bool(state.get("beginner_mode", False)):
                                st.caption(
                                    "Beginner Mode: draft generation is automatic; approve waves when ready."
                                )

    # If generation is in progress, poll once per rerun.
    if state.get("generate_job_id") and not state.get("checkpoint_a"):
        checkpoint_dir = Path(str(state.get("checkpoint_dir") or "out/checkpoints")).expanduser().resolve()
        job, checkpoint_a = poll_generate_dbt_checkpoint_a_job(
            checkpoint_dir=checkpoint_dir,
            job_id=str(state["generate_job_id"]),
        )
        status = str(job.get("status") or "")
        completed = int(job.get("completed_models") or 0)
        total = int(job.get("total_models") or 0)
        if checkpoint_a is not None:
            state["checkpoint_a"] = checkpoint_a
            state["generate_job_id"] = None

            # Compute wave plan from planner (tables in waves -> model_names from artifacts by table_key)
            plan = AutonomousPlanner().plan_from_report(report, max_tables_per_wave=25, max_waves=50)
            wave_plan: dict[str, Any] = {}
            table_to_model = {a.table_key: a.model_name for a in checkpoint_a.generated_models}
            for w in plan.waves:
                wid = str(w.wave_id)
                tables = [t.full_name for t in w.tables if t.full_name in table_to_model]
                if not tables:
                    continue
                models = [table_to_model[t] for t in tables]
                wave_plan[wid] = {"models": models, "tables": tables}

            state["wave_plan"] = wave_plan
            artifacts_by_model = {a.model_name: a for a in checkpoint_a.generated_models}
            # Initialize statuses and SQL buffers for all models in scope.
            for wid in wave_plan.keys():
                state["wave_status"][wid] = "PENDING"
                for m in wave_plan[wid]["models"]:
                    state["model_status"][m] = "PENDING"
                    if m in artifacts_by_model:
                        state["edited_sql"].setdefault(m, artifacts_by_model[m].sql)
                        state["user_modified"].setdefault(m, False)
        else:
            if status.upper() == "FAILED":
                state["generate_job_id"] = None
                state["task_error"] = str(job.get("error") or "unknown job failure")
                st.error(state["task_error"])
                return
            st.info(
                "Generating Checkpoint A... "
                + (f"{completed}/{total}" if total else f"{completed}/?")
                + f" models completed (status={status})."
            )
            if total:
                st.progress(min(1.0, completed / max(1, total)))
            else:
                st.progress(0.0)

            # Show last progress event if available (helps diagnose “stuck” vs “waiting”).
            events_path = checkpoint_dir / "jobs" / f"{state['generate_job_id']}.events.jsonl"
            if events_path.is_file():
                try:
                    last_line = [ln for ln in events_path.read_text(encoding="utf-8").splitlines() if ln.strip()][-1]
                    last_evt = json.loads(last_line)
                    st.caption(f"Last event: `{last_evt.get('event_type')}` at `{last_evt.get('timestamp')}`")
                except Exception:
                    st.caption("Last event: (unavailable)")

            # Live agent "Thought" stream during Checkpoint A generation.
            thought_events: list[dict[str, Any]] = []
            if events_path.is_file():
                try:
                    lines = events_path.read_text(encoding="utf-8").splitlines()
                    start_idx = int(state.get("events_lines_seen") or 0)
                    new_lines = lines[start_idx:]
                    state["events_lines_seen"] = len(lines)
                    for ln in new_lines:
                        if not ln.strip():
                            continue
                        try:
                            evt = json.loads(ln)
                        except json.JSONDecodeError:
                            continue
                        if str(evt.get("event_type") or "").upper() == "THOUGHT":
                            if isinstance(evt, dict):
                                thought_events.append(evt)
                except Exception:
                    thought_events = []

            if thought_events:
                with st.status("Collaborations & Reasoning", expanded=True) as status:
                    for evt in thought_events[-15:]:
                        agent_role = str(evt.get("agent_role") or "Agent")
                        msg = str(evt.get("message") or "").strip()
                        status.write(f"{agent_role}: {msg}")

            if st.button("Refresh status", key="dbt_mig_refresh_generate_job"):
                st.rerun()
            if bool(state.get("auto_refresh_jobs", True)):
                time.sleep(1.0)
                st.rerun()
            return

    if not state.get("checkpoint_a"):
        st.info("Generate Checkpoint A to start the migration orchestration.")
        return

    checkpoint_a = state["checkpoint_a"]
    if bool(getattr(checkpoint_a, "auth_error_detected", False)):
        st.warning("OpenAIAuthError detected. Running in deterministic fallback mode.")
    if bool(getattr(checkpoint_a, "rate_limit_detected", False)):
        st.warning("OpenAI rate limit detected. Running in fallback mode for stability.")

    # Wave tracker
    wave_ids = sorted(state.get("wave_plan") or {}).copy()
    selected_wave = state.get("selected_wave_id")
    if selected_wave is not None:
        sel = str(selected_wave)
        if sel in (state.get("wave_plan") or {}):
            wave_ids = [sel]
    st.markdown("### Wave Progress Tracker")
    if not wave_ids:
        st.warning("No waves found in this report (enable discovery-mode ingestion and lineages).")
        return

    # Display wave statuses as a dataframe
    wave_rows = []
    for wid in wave_ids:
        wave_rows.append(
            {
                "Wave": wid,
                "Status": state["wave_status"].get(wid, "PENDING"),
                "Models": ", ".join(state["wave_plan"][wid]["models"]),
            }
        )
    st.dataframe(pd.DataFrame(wave_rows), use_container_width=True, hide_index=True)

    # Helper: recompute wave statuses from model statuses.
    def _recompute_wave_statuses() -> None:
        for wid in wave_ids:
            models = state["wave_plan"][wid]["models"]
            model_states = [state["model_status"].get(m) for m in models]
            if any(s == "HITL_REQUIRED" for s in model_states):
                state["wave_status"][wid] = "HITL_REQUIRED"
            elif all(s in {"SUCCESS"} for s in model_states):
                state["wave_status"][wid] = "SUCCESS"
            elif any(s == "FAILED" for s in model_states):
                state["wave_status"][wid] = "FAILED"
            else:
                state["wave_status"][wid] = state["wave_status"].get(wid, "PENDING") or "PENDING"

    # Poll wave execution job (Checkpoint B orchestration) without blocking UI thread.
    if state.get("wave_exec_job_id") and state.get("wave_exec_job_wave_id"):
        job_id = str(state["wave_exec_job_id"])
        wave_id_for_job = str(state["wave_exec_job_wave_id"])
        wave_job_dir = Path(str(state.get("checkpoint_dir") or "out/checkpoints")).expanduser().resolve() / "jobs" / "wave_exec"
        job_file = wave_job_dir / f"{job_id}.json"
        result_file = wave_job_dir / f"{job_id}.result.json"
        if job_file.is_file():
            try:
                job_payload = json.loads(job_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                job_payload = {}
            status = str(job_payload.get("status") or "").upper()
            if status == "SUCCESS" and result_file.is_file():
                try:
                    result_payload = json.loads(result_file.read_text(encoding="utf-8"))
                    for mtrace in result_payload.get("model_results") or []:
                        if not isinstance(mtrace, dict):
                            continue
                        mn = str(mtrace.get("model_name") or "").strip()
                        stval = str(mtrace.get("state") or "").strip()
                        if mn:
                            state["model_status"][mn] = stval
                    _recompute_wave_statuses()
                finally:
                    state["wave_exec_job_id"] = None
                    state["wave_exec_job_wave_id"] = None
            elif status == "FAILED":
                state["wave_status"][wave_id_for_job] = "FAILED"
                state["wave_exec_job_id"] = None
                state["wave_exec_job_wave_id"] = None
                st.error(f"Wave execution failed: {job_payload.get('error') or 'unknown error'}")
            else:
                st.info(f"Wave {wave_id_for_job} executing... (job {job_id})")
                if bool(state.get("auto_refresh_jobs", True)):
                    time.sleep(1.0)
                    st.rerun()

    _recompute_wave_statuses()

    # Find blocking models (if any require HITL)
    hitl_models = [m for m, s in state["model_status"].items() if s == "HITL_REQUIRED"]

    beginner_mode = bool(state.get("beginner_mode", False))
    st.divider()
    st.markdown(
        "### Checkpoint A (Draft generation) - Beginner"
        if beginner_mode
        else "### Checkpoint A (Schema Review) - Ops Console"
    )

    checkpoint_dir = Path(str(state.get("checkpoint_dir") or "out/checkpoints")).expanduser().resolve()
    artifacts_by_model = {a.model_name: a for a in getattr(checkpoint_a, "generated_models", []) or []}

    if beginner_mode:
        total_models = len(artifacts_by_model)
        hitl_needed = sum(1 for m in state["model_status"].values() if m == "HITL_REQUIRED")
        st.write(f"Draft ready for **{total_models}** models.")
        if hitl_needed:
            st.warning(f"Some models require manual Fix (HITL_REQUIRED): **{hitl_needed}**.")
        st.info("Beginner flow: approve a wave to run dbt, then fix HITL_REQUIRED models in Checkpoint B.")
        # Show operator-friendly list of models in the selected wave(s).
        if wave_ids:
            for wid in wave_ids:
                wave_models: list[dict[str, Any]] = []
                for model_name in state["wave_plan"].get(wid, {}).get("models") or []:
                    if model_name not in artifacts_by_model:
                        continue
                    wave_models.append(
                        {
                            "Model": model_name,
                            "State": state["model_status"].get(model_name, "PENDING"),
                            "Needs Review": getattr(artifacts_by_model[model_name], "review_required", False),
                        }
                    )
                if wave_models:
                    st.markdown(f"#### Wave {wid} models")
                    st.dataframe(
                        pd.DataFrame(wave_models),
                        use_container_width=True,
                        hide_index=True,
                    )
    else:
        all_model_names = [m for wid in wave_ids for m in (state["wave_plan"].get(wid, {}).get("models") or [])]
        # Filter to models that actually exist in checkpoint artifacts.
        all_model_names = [m for m in all_model_names if m in artifacts_by_model]

        # Ensure controller state exists.
        state.setdefault("selected_model_name", all_model_names[0] if all_model_names else "")
        selected_model_name = state.get("selected_model_name") or (all_model_names[0] if all_model_names else "")

        # Build compact models grid (summary only).
        grid_rows: list[dict[str, Any]] = []
        for wid in wave_ids:
            for model_name in state["wave_plan"][wid]["models"]:
                if model_name not in artifacts_by_model:
                    continue
                model_art = artifacts_by_model[model_name]
                conf = float(getattr(model_art, "generation_confidence", 0.0) or 0.0)
                grid_rows.append(
                    {
                        "Wave": wid,
                        "Model": model_name,
                        "Mode": getattr(model_art, "generation_mode", "legacy"),
                        "Confidence": conf,
                        "State": state["model_status"].get(model_name, "PENDING"),
                        "Needs Review": getattr(model_art, "review_required", False),
                    }
                )

        if grid_rows:
            grid_df = pd.DataFrame(grid_rows)
            # Render as a table and a selection controller.
            st.dataframe(grid_df, use_container_width=True, hide_index=True)
            picked = st.selectbox(
                "Selected model (details panel)",
                options=all_model_names,
                index=max(0, all_model_names.index(selected_model_name))
                if selected_model_name in all_model_names
                else 0,
            )
            state["selected_model_name"] = picked
            selected_model_name = picked
        else:
            st.caption("No models found for the current wave scope.")

        def _render_dbt_model_details(model_name: str) -> None:
            model_art = artifacts_by_model.get(model_name)
            if model_art is None:
                return
            user_modified = bool(state["user_modified"].get(model_name))
            conf = float(getattr(model_art, "generation_confidence", 0.0) or 0.0)
            badge = _badge_for_model(model_art, user_modified)
            st.markdown(f"#### Model: `{model_name}` {badge}")
            st.markdown(f"**Confidence:** {_confidence_style(conf)} `{conf:.0%}`")

            edited = st.text_area(
                "SQL (editable)",
                value=str(state["edited_sql"].get(model_name) or model_art.sql),
                height=200,
                key=f"sql_edit_details_{model_name}",
            )
            state["edited_sql"][model_name] = edited
            state["user_modified"][model_name] = edited.strip() != str(model_art.sql).strip()

            with st.expander("Behind the Scenes", expanded=False):
                st.markdown("**Schema Agent Reasoning**")
                st.write(
                    str(getattr(model_art, "schema_agent_reasoning", "") or "Agent Thought Process pending.")
                )
                st.markdown("**dbt Agent Reasoning**")
                st.write(
                    str(getattr(model_art, "dbt_agent_reasoning", "") or "Agent Thought Process pending.")
                )
                st.markdown("**Semantic Mapping 2.0**")
                decision_tag = str(getattr(model_art, "mapping_decision_tag", "HUMAN_REQUIRED"))
                label = "[AI-AUTONOMOUS]" if decision_tag == "AI_AUTONOMOUS" else "[HUMAN-REQUIRED]"
                st.write(
                    f"{label} {str(getattr(model_art, 'translation_rationale', '') or 'No translation rationale captured.')}"
                )

                critical_reason = str(getattr(model_art, "critical_reason", "") or "").strip()
                if critical_reason:
                    st.markdown("**CRITICAL_REASON (HITL)**")
                    st.code(critical_reason)

            # Mapping table (field-level confidence preview).
            rows = model_art.mapping_rows or []
            if rows:
                mapped_df = pd.DataFrame(
                    [
                        {
                            "Hebrew": r.hebrew_name,
                            "English": r.english_alias,
                            "Confidence": float(r.confidence) if r.confidence is not None else 0.0,
                            "Source": r.source.value,
                            "Warnings": ",".join(r.warning_flags or []),
                            "Low Confidence": "⚠️ Low Confidence"
                            if ((float(r.confidence) if r.confidence is not None else 0.0) < 0.8)
                            else "",
                        }
                        for r in rows
                    ]
                )
                st.dataframe(mapped_df, use_container_width=True, hide_index=True)
            else:
                st.caption("No mapping rows for this model.")

            st.markdown("**Risk Meter & Scenarios**")
            if st.button(f"Analyze Risk/Scenarios ({model_name})", key=f"risk_scen_details_{model_name}"):
                insights_row = analyze_model_risk_and_scenarios(
                    checkpoint_dir=checkpoint_dir,
                    model_name=model_name,
                    sql=edited,
                )
                checkpoint_a.model_insights.setdefault("models", {})[model_name] = insights_row
                st.success("Risk and scenarios updated.")
                st.rerun()

            model_insight = (checkpoint_a.model_insights.get("models") or {}).get(model_name, {})
            risk_block = model_insight.get("risk") if isinstance(model_insight, dict) else {}
            risk_level = str((risk_block or {}).get("risk_level") or "Unknown")
            st.write(f"Risk Meter: **{risk_level}**")
            concerns = (risk_block or {}).get("concerns") or []
            for c in concerns[:4]:
                st.markdown(f"- {c}")
            scenarios = model_insight.get("scenarios") if isinstance(model_insight, dict) else []
            if scenarios:
                st.caption("Scenario Agent test ideas")
                for sc in scenarios[:3]:
                    st.markdown(f"- {sc}")

            st.markdown("**Synthetic Data Augmentation**")
            complexity = float(getattr(model_art, "complexity_score", 0.0) or 0.0)
            st.write(f"Complexity Score: `{complexity:.2f}`")
            approve_synth = st.checkbox(
                f"Approve Data-Gen for {model_name}",
                value=False,
                key=f"approve_synth_details_{model_name}",
            )
            row_cap = st.number_input(
                f"Row cap ({model_name})",
                min_value=1,
                max_value=50,
                value=10,
                key=f"synth_rowcap_details_{model_name}",
            )
            if st.button(f"Generate Synthetic Data ({model_name})", key=f"synth_gen_details_{model_name}"):
                rc, msg, synth_path = generate_synthetic_data_for_model(
                    checkpoint_dir=checkpoint_dir,
                    model_name=model_name,
                    schema_columns=[r.english_alias for r in rows] if rows else [],
                    approved=approve_synth,
                    row_cap=int(row_cap),
                )
                if rc == 0:
                    checkpoint_a.synthetic_dataset_paths[model_name] = synth_path
                    st.success(msg)
                    st.rerun()
                st.error(msg)

            if st.toggle(
                f"View Synthetic Dataset ({model_name})",
                value=False,
                key=f"show_synth_details_{model_name}",
            ):
                synth_path = checkpoint_a.synthetic_dataset_paths.get(model_name) or ""
                if synth_path and Path(synth_path).is_file():
                    st.code(Path(synth_path).read_text(encoding="utf-8"), language="json")
                else:
                    st.caption("No synthetic dataset generated yet.")

            st.markdown("**Chat with Model Agent**")
            q = st.text_input(f"Ask model agent ({model_name})", "", key=f"chat_q_details_{model_name}")
            if st.button(
                f"Send Chat Prompt ({model_name})",
                key=f"chat_send_details_{model_name}",
            ) and q.strip():
                chat_out = propose_sql_patch_from_chat(
                    checkpoint_dir=checkpoint_dir,
                    model_name=model_name,
                    sql=edited,
                    question=q.strip(),
                )
                st.info(chat_out.get("answer") or "No answer returned.")
                st.code(chat_out.get("sql_patch_proposal") or "-- no proposal", language="sql")
                st.caption("Patch proposal is manual-apply only.")

        if selected_model_name:
            _render_dbt_model_details(selected_model_name)

    # Wave-level approve controls (no per-model UI).
    st.markdown("### Wave Execution Controls")
    for wid in wave_ids:
        wave_entry = state["wave_plan"][wid]
        if not beginner_mode:
            wave_summary = {}
            if isinstance(getattr(checkpoint_a, "model_insights", {}), dict):
                wave_summary = (checkpoint_a.model_insights.get("waves") or {}).get(str(wid), {})
            st.markdown(f"#### Wave {wid} (Executive AI Summary)")
            st.write(wave_summary or "Run stress test to generate wave intelligence summary.")
            if st.button(f"Run AI Stress Test (Wave {wid})", key=f"wave_stress_{wid}"):
                summary = run_wave_stress_test(
                    checkpoint_dir=checkpoint_dir,
                    wave_id=str(wid),
                    model_names=list(wave_entry["models"]),
                    model_states=state.get("model_status", {}),
                )
                checkpoint_a.model_insights.setdefault("waves", {})[str(wid)] = summary
                st.success("Wave stress test complete.")
                st.rerun()

        prev_wid = str(int(wid) - 1)
        prev_ready = True
        if int(wid) > 0 and prev_wid in state["wave_status"]:
            prev_status = state["wave_status"].get(prev_wid)
            prev_ready = prev_status in {"SUCCESS", "PARTIAL"}
            if state.get("bypass_wave_id") is not None and int(prev_wid) == int(state["bypass_wave_id"]):
                st.warning(
                    f"WARNING: Wave {prev_wid} bypassed with incomplete models. Proceeding to Wave {int(prev_wid) + 1}."
                )
                prev_ready = True

        if not state["wave_status"].get(wid) or state["wave_status"].get(wid) == "PENDING":
            c1, c2 = st.columns(2)
            with c1:
                approve_key = f"approve_wave_{wid}"
                if st.button(f"Approve Wave {wid}", key=approve_key, disabled=not prev_ready):
                    st.session_state[f"confirm_approve_wave_{wid}"] = True
            with c2:
                confirm_key = f"confirm_approve_wave_{wid}"
                if st.session_state.get(confirm_key):
                    if st.button(f"Confirm Approve Wave {wid}", key=f"{confirm_key}_go"):
                        # Write edited files for wave models
                        models_dir = Path(str(state.get("output_dir") or "models/ama_generated")).expanduser().resolve()
                        artifacts = []
                        for model_name in wave_entry["models"]:
                            model_art = artifacts_by_model.get(model_name)
                            if model_art is None:
                                continue
                            model_art = model_art.model_copy(deep=True) if hasattr(model_art, "model_copy") else model_art
                            model_art.sql = str(state["edited_sql"].get(model_name) or model_art.sql)
                            artifacts.append(model_art)

                        write_model_artifacts(models_dir, artifacts)

                        # Execute wave in a background job and persist result for polling.
                        import uuid

                        state["wave_status"][wid] = "RUNNING"
                        dlq_dir = Path(str(state.get("dlq_dir") or "out/dbt_dlq")).expanduser().resolve()
                        checkpoint_dir = Path(str(state.get("checkpoint_dir") or "out/checkpoints")).expanduser().resolve()
                        dbt_project_dir = Path(str(state.get("dbt_project_dir") or ".")).expanduser().resolve()

                        job_id = str(uuid.uuid4())
                        state["wave_exec_job_id"] = job_id
                        state["wave_exec_job_wave_id"] = str(wid)
                        wave_job_dir = checkpoint_dir / "jobs" / "wave_exec"
                        wave_job_dir.mkdir(parents=True, exist_ok=True)
                        job_file = wave_job_dir / f"{job_id}.json"
                        result_file = wave_job_dir / f"{job_id}.result.json"

                        job_payload: dict[str, Any] = {
                            "status": "RUNNING",
                            "created_at": datetime.now(timezone.utc).isoformat(),
                            "wave_id": str(wid),
                        }
                        job_file.write_text(json.dumps(job_payload, indent=2, ensure_ascii=False), encoding="utf-8")

                        def _worker() -> None:
                            nonlocal job_payload
                            try:
                                res = execute_models_with_fix_loop(
                                    dbt_project_dir=dbt_project_dir,
                                    model_names=wave_entry["models"],
                                    max_attempts=3,
                                    dlq_dir=dlq_dir,
                                    checkpoint_dir=checkpoint_dir,
                                )
                                result_file.write_text(json.dumps(res.model_dump(mode="json"), ensure_ascii=False), encoding="utf-8")
                                job_payload["status"] = "SUCCESS"
                                job_payload["updated_at"] = datetime.now(timezone.utc).isoformat()
                            except Exception as exc:
                                job_payload["status"] = "FAILED"
                                job_payload["updated_at"] = datetime.now(timezone.utc).isoformat()
                                job_payload["error"] = str(exc)
                            finally:
                                job_file.write_text(json.dumps(job_payload, indent=2, ensure_ascii=False), encoding="utf-8")

                        t = threading.Thread(target=_worker, daemon=True)
                        t.start()

                        st.success(f"Wave {wid} execution started (job={job_id}). Refresh tab to poll completion.")
                        st.session_state[confirm_key] = False
                        st.rerun()
    st.divider()
    st.markdown("### Checkpoint B (Manual Fix) - HITL Required Models")
    if not hitl_models:
        st.caption("No models currently require Checkpoint B.")
    else:
        checkpoint_dir = Path(str(state.get("checkpoint_dir") or "out/checkpoints")).expanduser().resolve()
        dlq_dir = Path(str(state.get("dlq_dir") or "out/dbt_dlq")).expanduser().resolve()
        for model_name in hitl_models:
            cp_file = checkpoint_dir / f"checkpoint_b_{model_name}.json"
            if not cp_file.is_file():
                # runner uses model name sanitized; attempt both
                st.warning(f"Checkpoint file not found for {model_name} in {cp_file}")
                continue
            mtime = cp_file.stat().st_mtime if cp_file.is_file() else 0.0
            cp_payload = load_checkpoint_b_payload_cached(str(cp_file), float(mtime))
            st.markdown(f"#### {model_name}")
            if beginner_mode:
                err_log = str(cp_payload.get("error_log") or "")
                fix_analysis = str(cp_payload.get("fix_agent_error_analysis") or "")
                suggested_sql = str(cp_payload.get("suggested_sql") or "")

                st.caption("Fix required (Checkpoint B)")
                if err_log.strip():
                    st.code(err_log[:600], language="text")
                if fix_analysis.strip():
                    st.markdown("**Why the fix is needed**")
                    st.write(fix_analysis)

                if st.button(f"Apply AI Fix ({model_name})", key=f"beginner_apply_ai_{model_name}"):
                    if not suggested_sql.strip():
                        st.error("No AI suggested SQL is available in this checkpoint artifact.")
                    else:
                        dbt_project_dir = Path(str(state.get("dbt_project_dir") or ".")).expanduser().resolve()
                        rc, msg = apply_ai_fix_from_checkpoint(
                            dbt_project_dir=dbt_project_dir,
                            checkpoint_dir=checkpoint_dir,
                            model_name=model_name,
                            ai_sql=suggested_sql,
                        )
                        if rc == 0:
                            state["model_status"][model_name] = "SUCCESS"
                            _recompute_wave_statuses()
                            st.success(msg)
                            st.rerun()
                        st.error(msg)

                dlq_confirm_key = f"beginner_dlq_confirm_{model_name}"
                if st.button(f"Route to DLQ ({model_name})", key=f"beginner_route_dlq_{model_name}"):
                    st.session_state[dlq_confirm_key] = True
                if st.session_state.get(dlq_confirm_key):
                    if st.button(f"Confirm DLQ Routing ({model_name})", key=f"{dlq_confirm_key}_go"):
                        reject_checkpoint_b_to_dlq(
                            model_name=model_name,
                            checkpoint_dir=checkpoint_dir,
                            dlq_dir=dlq_dir,
                        )
                        state["model_status"][model_name] = "FAILED"
                        _recompute_wave_statuses()
                        st.session_state[dlq_confirm_key] = False
                        st.rerun()
                # Skip the advanced editor/diff UI in Beginner Mode.
                continue

            st.code(str(cp_payload.get("error_log") or ""), language="text")
            if cp_payload.get("auth_error"):
                st.warning("OpenAIAuthError detected for Fix Agent. Fallback mode active.")
            if cp_payload.get("rate_limit_error"):
                st.warning("OpenAI rate-limit detected for Fix Agent. Fallback mode active.")
            st.markdown("**Fix Agent Error Analysis**")
            st.write(str(cp_payload.get("fix_agent_error_analysis") or "No analysis captured."))
            left, right = st.columns(2)
            with left:
                st.caption("Failed SQL")
                st.code(str(cp_payload.get("failed_sql") or cp_payload.get("current_sql") or ""), language="sql")
            with right:
                st.caption("AI Suggested Fix")
                st.code(str(cp_payload.get("suggested_sql") or ""), language="sql")
            fixed_sql = st.text_area(
                "Current failing SQL (editable)",
                value=str(cp_payload.get("current_sql") or ""),
                height=160,
                key=f"cpb_sql_{model_name}",
            )
            hist = cp_payload.get("attempt_history") or []
            if hist:
                st.caption("Attempt history")
                st.dataframe(pd.DataFrame(hist), use_container_width=True, hide_index=True)

            col1, col2 = st.columns(2)
            with col1:
                cpb_confirm_key = f"cpb_confirm_fix_{model_name}"
                if st.button(f"Approve with Fix ({model_name})", key=f"cpb_ap_{model_name}"):
                    st.session_state[cpb_confirm_key] = True
                if st.button(f"Apply AI Fix ({model_name})", key=f"cpb_apply_ai_{model_name}"):
                    dbt_project_dir = Path(str(state.get("dbt_project_dir") or ".")).expanduser().resolve()
                    rc, msg = apply_ai_fix_from_checkpoint(
                        dbt_project_dir=dbt_project_dir,
                        checkpoint_dir=checkpoint_dir,
                        model_name=model_name,
                        ai_sql=str(cp_payload.get("suggested_sql") or ""),
                    )
                    if rc == 0:
                        state["model_status"][model_name] = "SUCCESS"
                        _recompute_wave_statuses()
                        st.success(msg)
                        st.rerun()
                    st.error(msg)
                if st.session_state.get(cpb_confirm_key):
                    if st.button(f"Confirm Fix Apply ({model_name})", key=f"{cpb_confirm_key}_go"):
                        # overwrite SQL
                        models_dir = Path(str(state.get("output_dir") or "models/ama_generated")).expanduser().resolve()
                        target_sql = models_dir / f"{model_name}.sql"
                        target_sql.write_text(str(fixed_sql), encoding="utf-8")
                        # rerun single model
                        dbt_project_dir = Path(str(state.get("dbt_project_dir") or ".")).expanduser().resolve()
                        with st.spinner("Re-running model with dbt..."):
                            result = execute_models_with_fix_loop(
                                dbt_project_dir=dbt_project_dir,
                                model_names=[model_name],
                                max_attempts=3,
                                dlq_dir=dlq_dir,
                                checkpoint_dir=checkpoint_dir,
                            )
                        for tr in result.model_results:
                            state["model_status"][tr.model_name] = tr.state.value
                        _recompute_wave_statuses()
                        st.session_state[cpb_confirm_key] = False
                        st.rerun()
            with col2:
                cpb_reject_confirm_key = f"cpb_confirm_reject_{model_name}"
                if st.button(f"Route to DLQ ({model_name})", key=f"cpb_rj_{model_name}"):
                    st.session_state[cpb_reject_confirm_key] = True
                if st.session_state.get(cpb_reject_confirm_key):
                    if st.button(f"Confirm DLQ Routing ({model_name})", key=f"{cpb_reject_confirm_key}_go"):
                        reject_checkpoint_b_to_dlq(
                            model_name=model_name,
                            checkpoint_dir=checkpoint_dir,
                            dlq_dir=dlq_dir,
                        )
                        state["model_status"][model_name] = "FAILED"
                        _recompute_wave_statuses()
                        st.session_state[cpb_reject_confirm_key] = False
                        st.rerun()

    st.divider()
    st.markdown("### AI Telemetry & Cost Dashboard")
    telemetry_rows: list[dict[str, Any]] = []
    for model_art in checkpoint_a.generated_models:
        for row in getattr(model_art, "ai_telemetry", []) or []:
            if isinstance(row, dict):
                telemetry_rows.append(row)
    by_agent: dict[str, int] = {}
    total_tokens = 0
    for row in telemetry_rows:
        agent = str(row.get("agent_name") or "unknown")
        tokens = int(row.get("tokens_used") or 0)
        by_agent[agent] = by_agent.get(agent, 0) + tokens
        total_tokens += tokens
    fallback_models = sum(1 for m in checkpoint_a.generated_models if str(getattr(m, "generation_mode", "legacy")) != "ai")
    ai_models = len(checkpoint_a.generated_models) - fallback_models
    fix_first_try_den = 0
    fix_first_try_ok = 0
    for status in state["model_status"].values():
        if status in {"SUCCESS", "FAILED", "HITL_REQUIRED"}:
            fix_first_try_den += 1
            if status == "SUCCESS":
                fix_first_try_ok += 1
    c1, c2, c3 = st.columns(3)
    c1.metric("Total Tokens", f"{total_tokens}")
    c2.metric("Estimated Cost", f"${_estimate_cost(total_tokens):.4f}")
    c3.metric("AI Success Rate", f"{(ai_models / max(1, len(checkpoint_a.generated_models))):.0%}")
    st.metric("Fallback Rate", f"{(fallback_models / max(1, len(checkpoint_a.generated_models))):.0%}")
    st.metric("Fix-it Rate (First Try)", f"{(fix_first_try_ok / max(1, fix_first_try_den)):.0%}")
    if by_agent:
        st.dataframe(
            pd.DataFrame([{"Agent": k, "Tokens": v} for k, v in sorted(by_agent.items())]),
            use_container_width=True,
            hide_index=True,
        )

    st.divider()
    st.markdown("### DLQ Viewer")
    dlq_dir = Path(str(state.get("dlq_dir") or "out/dbt_dlq")).expanduser().resolve()
    dlq_path = dlq_dir / "dlq_records.jsonl"
    if not dlq_path.is_file():
        st.info("No DLQ records yet.")
    else:
        mtime = dlq_path.stat().st_mtime if dlq_path.is_file() else 0.0
        dlq_rows = load_dlq_rows_cached(str(dlq_path), float(mtime))
        if dlq_rows:
            df = pd.DataFrame(dlq_rows)
            search = st.text_input("Search DLQ", "", key="dlq_search")
            if search.strip():
                mask = df.astype(str).apply(lambda s: s.str.contains(search, case=False, na=False)).any(axis=1)
                df = df[mask]
            st.dataframe(df, use_container_width=True, hide_index=True)
            if not df.empty:
                st.download_button(
                    "Download DLQ JSONL",
                    data="\n".join(json.dumps(r, ensure_ascii=False) for r in dlq_rows).encode("utf-8"),
                    file_name="dlq_records.jsonl",
                    mime="text/plain",
                )


def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


@st.cache_data(show_spinner=False)
def load_checkpoint_b_payload_cached(path_str: str, mtime: float) -> dict[str, Any]:
    # `mtime` is part of the cache key to avoid stale reads after writes.
    _ = mtime
    return json.loads(Path(path_str).read_text(encoding="utf-8"))


@st.cache_data(show_spinner=False)
def load_dlq_rows_cached(path_str: str, mtime: float) -> list[dict[str, Any]]:
    # `mtime` is part of the cache key to avoid stale reads after writes.
    _ = mtime
    out: list[dict[str, Any]] = []
    p = Path(path_str)
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


@st.cache_data(show_spinner=False)
def load_report_cached(path_str: str, mtime_report: float, reload_bust: int) -> dict[str, Any]:
    """
    Raw report JSON.

    **Cache key must include** `mtime_report` and `reload_bust`. Parameters prefixed with `_`
    are excluded from Streamlit's cache hash, so a previous `_mtime_report` arg never
    invalidated when the file changed — only `path_str` was keyed.
    """
    _ = (mtime_report, reload_bust)  # participate in cache key only
    return load_report_json(path_str)


def _hitl_path(report_path: Path) -> Path:
    return report_path.with_suffix(".hitl.json")


def _load_hitl(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"version": 1, "decisions": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "decisions": {}}


def _save_hitl(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _confidence_gauge(val: float, *, key: str) -> None:
    """
    Arc-only gauge: Plotly's gauge+number mode draws the value on top of the arc in narrow
    columns (Streamlit expanders), so we show the number in a separate st.metric below.
    """
    if go is None:
        st.metric("Confidence", f"{val:.0%}")
        return
    color = "#2ca02c" if val >= 0.8 else ("#ffbf00" if val >= 0.4 else "#d62728")
    pct = min(100.0, max(0.0, val * 100.0))
    fig = go.Figure(
        go.Indicator(
            mode="gauge",
            value=pct,
            title={"text": ""},
            gauge={
                "axis": {"range": [0, 100]},
                "bar": {"color": color},
                "steps": [
                    {"range": [0, 40], "color": "#ffcccc"},
                    {"range": [40, 80], "color": "#fff4cc"},
                    {"range": [80, 100], "color": "#ccffcc"},
                ],
            },
        )
    )
    fig.update_layout(height=170, margin=dict(l=10, r=10, t=20, b=10))
    st.plotly_chart(fig, use_container_width=True, key=key)
    st.metric("Confidence", f"{pct:.0f}%", help="Merge confidence (0–100%)")


def _glossary_card_container():
    """Prefer bordered cards (Streamlit ≥1.33); older versions use a plain container."""
    try:
        return st.container(border=True)  # type: ignore[call-arg]
    except TypeError:
        return st.container()


def _merge_conf_float(ent: dict[str, Any]) -> float:
    try:
        return float((ent or {}).get("merge_confidence", 0.0))
    except (TypeError, ValueError):
        return 0.0


def _sync_tbl_pick_from_dataframe(
    state: Any,
    df_view: pd.DataFrame,
    *,
    valid_tables: set[str],
    session_key: str,
) -> None:
    """When user selects a row in the inventory grid, set session_key to that table's full_name."""
    if state is None:
        return
    try:
        sel = getattr(state, "selection", None)
        if sel is None and isinstance(state, dict):
            sel = state.get("selection")
        if not sel:
            return
        rows = getattr(sel, "rows", None)
        if rows is None and isinstance(sel, dict):
            rows = sel.get("rows", [])
        if not rows:
            return
        idx = int(rows[0])
        dfv = df_view.reset_index(drop=True)
        if idx < 0 or idx >= len(dfv) or "full_name" not in dfv.columns:
            return
        fn = str(dfv.iloc[idx]["full_name"]).strip()
        if fn and fn in valid_tables:
            st.session_state[session_key] = fn
    except (TypeError, ValueError, KeyError, IndexError, AttributeError):
        return


def _ask_match_type_label(role: str) -> str:
    r = (role or "").lower().strip()
    if r == "confirmed":
        return "Confirmed (merged)"
    if r == "review":
        return "Review (pending)"
    if r == "importance_tracked":
        return "Importance (tracked)"
    return (role or "").strip() or "—"


def _table_max_merge_confidence(
    merged_all: list[Any],
    review_all: list[Any],
    trash_all: list[Any],
) -> dict[str, float]:
    """Highest merge_confidence per source_table across filtered merge buckets."""
    out: dict[str, float] = {}
    for e in merged_all + review_all + trash_all:
        if not isinstance(e, dict):
            continue
        stbl = str(e.get("source_table") or "").strip()
        if not stbl:
            continue
        c = _merge_conf_float(e)
        out[stbl] = max(out.get(stbl, 0.0), c)
    return out


def main() -> None:
    st.set_page_config(page_title="AMA System Migration", layout="wide")
    st.title("AMA System Migration Dashboard")
    st.caption("Environment-wide discovery, domains, and waves — same JSON contract as Excel export.")

    default_path = get_env("AMA_REPORT_PATH", "").strip()
    report_path_resolved: Path | None = None

    with st.sidebar:
        st.header("Report")
        if "report_reload_bust" not in st.session_state:
            st.session_state.report_reload_bust = 0
        uploaded = st.file_uploader("Or upload JSON", type=["json"], key="json_up")
        path_in = st.text_input("Report path", value=default_path, placeholder="path/to/report.json")

        raw_report: dict[str, Any]
        if uploaded is not None:
            try:
                raw_report = json.loads(uploaded.getvalue().decode("utf-8"))
            except json.JSONDecodeError as e:
                st.error(f"Invalid JSON: {e}")
                return
            report_path_resolved = None
        else:
            path = path_in.strip() or None
            if not path:
                st.info("Set AMA_REPORT_PATH, enter a path, or upload a JSON file.")
                return
            try:
                report_path_resolved = Path(path).resolve()
                raw_report = load_report_cached(
                    str(report_path_resolved),
                    _safe_mtime(report_path_resolved),
                    int(st.session_state.report_reload_bust),
                )
            except OSError as e:
                st.error(f"Cannot read report: {e}")
                return
            except json.JSONDecodeError as e:
                st.error(f"Invalid JSON: {e}")
                return
            if not path_in.strip().lower().endswith(".json"):
                st.warning(
                    "Report path should end in **.json** (e.g. `report.json`). "
                    "A `.js` path will not load the ingestion report."
                )

        hitl_file = _hitl_path(report_path_resolved) if report_path_resolved else None
        rp_key = str(report_path_resolved) if report_path_resolved else "upload"
        if st.session_state.get("hitl_report_key") != rp_key:
            st.session_state.hitl_report_key = rp_key
            if hitl_file and hitl_file.is_file():
                st.session_state.hitl = _load_hitl(hitl_file)
            else:
                st.session_state.hitl = {"version": 1, "decisions": {}}

        report = apply_hitl_to_report(raw_report, st.session_state.hitl)
        _disc0 = report.get("discovery") or {}
        _es0 = _disc0.get("executive_summary") or {}
        if not (_es0.get("risk_hotspots") or []) and (report.get("lineage") or {}).get("edges"):
            enrich_executive_risk_hotspots(report)

        _sv_disp = str(report.get("schema_version") or "").strip() or "— (legacy)"
        st.metric("Schema version", _sv_disp)
        if _sv_disp == "1.1":
            st.success("Schema Version: **1.1**", icon="✅")
        else:
            st.caption(f"Legacy or unknown schema (`{_sv_disp}`). Regenerate with `ama-ingest run` for full v1.1.")

        if report_path_resolved and st.button("Reload from Disk", key="reload_report"):
            st.session_state.report_reload_bust = int(st.session_state.report_reload_bust) + 1
            st.session_state.hitl_report_key = ""
            load_report_cached.clear()
            st.session_state.hitl_report_key = ""
            st.rerun()

        st.header("Filters")
        disc = report.get("discovery") or {}
        inv_df = _inventory_df(report)
        if inv_df.empty or "business_domain" not in inv_df.columns:
            domain_opts: list[str] = []
        else:
            domain_opts = sorted({str(x) for x in inv_df["business_domain"].dropna().unique()})
        domains = st.multiselect("Business domain", options=domain_opts, default=[])
        portfolio = st.selectbox("Portfolio section", options=["All", "Core Business", "Technical Debt"])
        st.caption(
            "**Merge confidence** (readiness): **Executive** scatter (x-axis), **Glossary** summary column + gauge per row, "
            "**Tables** Confirmed/Review **Confidence** columns."
        )

    _report_identity = (
        f"{report_path_resolved!s}:{st.session_state.report_reload_bust}"
        if report_path_resolved
        else "upload"
    )
    if st.session_state.get(SCALE_ENGINE_REPORT_KEY) != _report_identity:
        st.session_state.pop(SCALE_ENGINE_CACHE_KEY, None)
        st.session_state.pop(SCALE_ENGINE_THRESHOLD_KEY, None)
        st.session_state[SCALE_ENGINE_REPORT_KEY] = _report_identity

    if "scale_conf_floor" not in st.session_state:
        st.session_state.scale_conf_floor = 90
    if "scale_crit_ceil" not in st.session_state:
        st.session_state.scale_crit_ceil = 40
    if "migration_dialect" not in st.session_state:
        st.session_state.migration_dialect = "duckdb"
    if "migrated_tables" not in st.session_state:
        st.session_state.migrated_tables = []
    dashboard_dbt_project_dir = Path(
        str((st.session_state.get("migration_agent") or {}).get("dbt_project_dir") or ".")
    ).expanduser().resolve()
    dashboard_output_dir = _resolve_output_dir(report_path_resolved, dashboard_dbt_project_dir)
    _bulk_job_id, _bulk_job_state, _bulk_applied_now = _apply_bulk_completion_once(
        dbt_project_dir=dashboard_dbt_project_dir
    )

    _sv_main = str(report.get("schema_version") or "").strip() or "— (legacy)"
    st.markdown(f"**Report schema version:** `{_sv_main}`")
    _mctx = str(report.get("migration_context") or report.get("target_table") or "").strip()
    _ms = report.get("merge_scope") if isinstance(report.get("merge_scope"), dict) else {}
    _mstate = (report.get("discovery") or {}).get("migration_state") if isinstance(report.get("discovery"), dict) else {}
    _ndom = len(_mstate.get("domains_detected") or []) if isinstance(_mstate, dict) else 0
    _ninv = len(inv_df) if not inv_df.empty else 0
    st.markdown(
        f"**System overview:** **{_ninv}** table(s) in inventory"
        + (f", **{_ndom}** business domain(s) detected" if _ndom else "")
        + "."
    )
    if _ms.get("label"):
        st.caption(
            f"**Ingest scope:** {_ms.get('label')} · "
            f"DDL merges across **{_ms.get('tables_merged', '?')}** table(s). "
            f"**Migration context** (`{_mctx or '—'}`) is the comms/git anchor — not the merge limit."
        )

    exec_sum = disc.get("executive_summary") or {}
    domain_matrix = exec_sum.get("domain_matrix") or []

    inv_view = inv_df.copy()
    if domains and not inv_view.empty and "business_domain" in inv_view.columns:
        inv_view = inv_view[inv_view["business_domain"].isin(domains)]
    if portfolio and portfolio != "All" and not inv_view.empty and "portfolio_section" in inv_view.columns:
        inv_view = inv_view[inv_view["portfolio_section"] == portfolio]

    risk_set = _high_risk_tables(inv_view, report)
    am = report.get("alias_merge") or {}
    dom_filter = domains if domains else None
    merged_all, review_all, trash_all = _merge_rows_for_filters(
        report, domains=dom_filter, portfolio=portfolio, conf_min=_MERGE_CONF_SCOPE
    )

    allowed_tables = inventory_allowed_tables(inv_view)
    merged_all, review_all, trash_all = filter_merge_buckets_by_inventory(
        merged_all, review_all, trash_all, allowed_tables
    )
    pct_filtered = pct_confirmed_filtered(merged_all, review_all, trash_all)
    if not inv_view.empty and "query_count" in inv_view.columns:
        queries_matched_display = int(pd.to_numeric(inv_view["query_count"], errors="coerce").fillna(0).sum())
    else:
        queries_matched_display = int(report.get("queries_matched") or 0)

    hitl_file = _hitl_path(report_path_resolved) if report_path_resolved else None

    raw_glossary = build_business_glossary_entries(report)
    glossary = group_glossary_entries(raw_glossary)
    glossary = filter_glossary_grouped(
        glossary,
        report=report,
        domains=domains if domains else None,
        portfolio=portfolio,
        conf_min=_MERGE_CONF_SCOPE,
    )
    scatter_rows = build_impact_readiness_scatter_rows(report)
    if allowed_tables is not None and scatter_rows:
        scatter_rows = [
            r
            for r in scatter_rows
            if str(r.get("source_table") or "") in allowed_tables
        ]

    domain_matrix_filtered = filter_domain_matrix_rows(
        domain_matrix, domains=domains if domains else None
    )
    rh_exec_filtered = filter_risk_hotspots(
        exec_sum.get("risk_hotspots") or [], allowed_tables=allowed_tables
    )

    st.markdown("📊 Analysis")
    analysis_tabs = st.tabs(ANALYSIS_TABS)
    st.markdown("⚙️ Execution")
    execution_tabs = st.tabs(EXECUTION_TABS)
    if LAUNCHPAD_EXPANDED_KEY not in st.session_state:
        st.session_state[LAUNCHPAD_EXPANDED_KEY] = True
    with st.expander("Migration Launchpad", expanded=bool(st.session_state.get(LAUNCHPAD_EXPANDED_KEY, True))):
        lp1, lp2 = st.columns(2)
        with lp1:
            st.info("🎯 Migrate individual tables\n\nSelect a table in the Tables tab and click Migrate This Table.")
            if st.button("Go to Tables →", key="launchpad_go_tables"):
                st.session_state["analysis_focus_tab"] = "Tables"
        with lp2:
            st.info("⚡ Bulk migrate by confidence\n\nApprove all GREEN tables in one click from the Bulk Migration tab.")
            if st.button("Go to Bulk Migration →", key="launchpad_go_bulk"):
                st.session_state["analysis_focus_tab"] = "Bulk Migration"
        if st.button("Hide Launchpad", key="launchpad_hide"):
            st.session_state[LAUNCHPAD_EXPANDED_KEY] = False
            st.rerun()

    with analysis_tabs[0]:
        _ov_l, _ov_r = st.columns([6, 1])
        with _ov_r:
            if st.button("💬 Ask Agent", key="ask_agent_overview"):
                _set_agent_prefill("Summarize the migration readiness of this report.")
        st.caption(
            "Sidebar filters (**Business domain**, **Portfolio**) scope KPIs, charts, domain matrix, hotspots, "
            "Domains tab, search, glossary, and the **Tables** inventory list. "
            "**% Confirmed** counts only merge rows whose ``source_table`` appears in that **filtered inventory** "
            "(same scope as Domains / Tables). **Confidence** stays visible in the "
            "scatter (x-axis), glossary gauges, and table drill-downs — nothing is hidden by a global threshold."
        )
        col1, col2, col3 = st.columns(3)
        col1.metric("% Confirmed (filtered scope)", f"{pct_filtered:.1f}%")
        col2.metric("Queries matched (inventory scope)", queries_matched_display)
        col3.metric("Confirmed columns (filtered)", len(merged_all))

        st.divider()
        st.markdown("### Orchestration")
        if report_path_resolved is not None:
            confirm_key = "exec_full_ingest_confirm"
            if st.button("Run Full Ingest", key="exec_full_ingest_btn"):
                st.session_state[confirm_key] = True
            if st.session_state.get(confirm_key):
                if st.button("Confirm Run Full Ingest (overwrite report.json)", key="exec_full_ingest_go"):
                    cmd = [
                        "ama-ingest",
                        "run",
                        "--format",
                        "json",
                        "-o",
                        str(report_path_resolved),
                        "--discovery-mode",
                        "--discovery-merge-all",
                    ]
                    with st.spinner("Running `ama-ingest run`..."):
                        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
                    if proc.returncode == 0:
                        load_report_cached.clear()
                        st.session_state[confirm_key] = False
                        st.success("Ingest completed and dashboard refreshed.")
                        st.rerun()
                    else:
                        st.error(f"Ingest failed (rc={proc.returncode}): {proc.stderr[:800]}")
                        st.session_state[confirm_key] = False
        else:
            st.caption("Run Full Ingest disabled in upload mode (no local report path).")

        c1, c2 = st.columns(2)
        with c1:
            if go is not None:
                fig_g = go.Figure(
                    go.Indicator(
                        mode="gauge",
                        value=pct_filtered,
                        title={"text": "% Confirmed"},
                        gauge={"axis": {"range": [0, 100]}, "bar": {"color": "darkblue"}},
                    )
                )
                fig_g.update_layout(height=240, margin=dict(l=10, r=10, t=40, b=10))
                st.plotly_chart(fig_g, use_container_width=True, key="exec_gauge_pct_confirmed")
        with c2:
            st.markdown("### Impact vs. readiness")
            st.caption(
                "X-axis = **merge confidence** (readiness). Bubble size ~ query volume. "
                "Big green bubbles = high value + high confidence — migrate first."
            )
            if px is not None and scatter_rows:
                sdf = pd.DataFrame(scatter_rows)
                fig_s = px.scatter(
                    sdf,
                    x="confidence",
                    y="importance",
                    size="query_volume",
                    hover_name="label",
                    color="source_table",
                    title="Business value (importance) vs technical readiness (confidence)",
                    labels={
                        "confidence": "Confidence (readiness)",
                        "importance": "Importance (business value)",
                        "query_volume": "Query volume",
                    },
                )
                fig_s.update_layout(height=420)
                st.plotly_chart(fig_s, use_container_width=True, key="exec_scatter_impact_readiness")
            elif scatter_rows:
                st.dataframe(pd.DataFrame(scatter_rows), use_container_width=True, hide_index=True)
            else:
                st.info("No merged entities to plot.")

        if domain_matrix_filtered and px is not None:
            ddf = pd.DataFrame(domain_matrix_filtered)
            if not ddf.empty and "business_domain" in ddf.columns:
                fig_b = px.bar(
                    ddf,
                    x="business_domain",
                    y="business_importance",
                    color="migration_complexity",
                    labels={
                        "business_domain": "Domain",
                        "business_importance": "Importance (0–100)",
                        "migration_complexity": "Complexity",
                    },
                    title="Importance by business domain",
                )
                st.plotly_chart(fig_b, use_container_width=True, key="exec_bar_domain_importance")

        st.markdown("### Risk Hotspots (Blast Radius)")
        st.caption(
            "Tables with the highest downstream reach in the lineage graph: more domains touched "
            "and more co-queried neighbors imply a larger blast radius for migration changes."
        )
        if rh_exec_filtered:
            rdf = pd.DataFrame(rh_exec_filtered)
            display_cols = [c for c in ("table", "blast_radius_score", "domains_touched", "downstream_tables_reached") if c in rdf.columns]
            if display_cols:
                view = rdf[display_cols].copy()
                if "domains_touched" in view.columns:
                    view["domains_touched"] = view["domains_touched"].apply(
                        lambda x: ", ".join(x) if isinstance(x, list) else str(x)
                    )
                view = view.rename(
                    columns={
                        "table": "Table",
                        "blast_radius_score": "Blast radius score",
                        "domains_touched": "Domains touched",
                        "downstream_tables_reached": "Downstream tables (reach)",
                    }
                )
                st.dataframe(view, use_container_width=True, hide_index=True)
            else:
                st.dataframe(rdf, use_container_width=True, hide_index=True)
        elif exec_sum.get("risk_hotspots") and not rh_exec_filtered:
            st.info("No risk hotspots in the **current filter scope** — widen Business domain or clear Portfolio filters.")
        else:
            _disc_h = report.get("discovery") or {}
            _lin_h = report.get("lineage") or {}
            _edges_h = _lin_h.get("edges") or []
            if not _disc_h.get("enabled"):
                st.info(
                    "No **discovery** inventory in this report. Regenerate with "
                    "**`ama-ingest run --discovery-mode`** (plus your usual flags) so tables and domains exist."
                )
            elif not _edges_h:
                st.info(
                    "No **lineage** edges in this report. Regenerate with **`--discovery-mode`** so the "
                    "co-query graph can be built from SQL logs."
                )
            else:
                st.info(
                    "No risk hotspots were scored for this export: the lineage graph may be **sparse** or "
                    "no table had enough **cross-domain / neighbor reach** in this run. "
                    "Try richer SQL logs, or ask engineering to adjust hotspot thresholds. "
                    "The dashboard **recomputes** hotspots when you load a report when the JSON list is empty."
                )

    with analysis_tabs[1]:
        _dom_l, _dom_r = st.columns([6, 1])
        with _dom_r:
            if st.button("💬 Ask Agent", key="ask_agent_domains"):
                _set_agent_prefill("Which domain should I migrate first and why?")
        st.subheader("Domain deep dives")
        st.caption("Data health per domain — how ready the portfolio is to move (uses sidebar filters).")
        dlist = (
            sorted({str(x) for x in inv_view["business_domain"].dropna().unique()})
            if not inv_view.empty and "business_domain" in inv_view.columns
            else []
        )
        first_dom = next((d for d in dlist if d), None)
        for dom in dlist:
            if not dom:
                continue
            tables_dom: set[str] = set()
            if not inv_view.empty and "business_domain" in inv_view.columns and "full_name" in inv_view.columns:
                sub_dom = inv_view[inv_view["business_domain"] == dom]
                tables_dom = {str(x) for x in sub_dom["full_name"].dropna().unique()}
            dh = domain_data_health_filtered(
                report,
                dom,
                merged_all=merged_all,
                review_all=review_all,
                trash_all=trash_all,
                inventory_full_names_for_domain=tables_dom,
            )
            expanded = dom == "Finance" or ("Finance" not in dlist and dom == first_dom)
            with st.expander(
                f"**{dom}** — {dh['table_count']} tables",
                expanded=expanded,
            ):
                m1, m2, m3 = st.columns(3)
                m1.metric("Columns confirmed", f"{dh['pct_columns_confirmed']:.1f}%")
                m2.metric("Avg importance", f"{dh['avg_importance']:.4f}")
                m3.metric("Risk level", dh["risk_level"])
                st.caption(
                    f"Confirmed: {dh['n_confirmed']} · Review: {dh['n_review']} · Trash: {dh['n_trash']}"
                )
                sub = inv_view[inv_view["business_domain"] == dom] if not inv_view.empty else pd.DataFrame()
                if not sub.empty and "full_name" in sub.columns:
                    st.dataframe(
                        sub[
                            [
                                c
                                for c in (
                                    "full_name",
                                    "query_count",
                                    "priority_score",
                                    "business_description",
                                )
                                if c in sub.columns
                            ]
                        ],
                        use_container_width=True,
                        hide_index=True,
                    )

    with analysis_tabs[6]:
        _render_planner_tab(report)

    with analysis_tabs[3]:
        _gl_l, _gl_r = st.columns([6, 1])
        with _gl_r:
            if st.button("💬 Ask Agent", key="ask_agent_glossary"):
                _set_agent_prefill("Are there any unmapped columns I should resolve before migrating?")
        st.subheader("Business Translator — glossary")
        gs_inv = report.get("glossary_source") or {}
        gs_flat: list[dict[str, Any]] = []
        for layer in gs_inv.get("layers") or []:
            if not isinstance(layer, dict):
                continue
            fn = str(layer.get("file") or "")
            for ent in layer.get("entries") or []:
                if isinstance(ent, dict):
                    gs_flat.append(
                        {
                            "file": fn,
                            "source_term": ent.get("source_term"),
                            "target_column": ent.get("target_column"),
                        }
                    )
        if gs_flat:
            with st.expander(
                f"Full glossary inventory from sample_data ({len(gs_flat)} rows in JSON files)",
                expanded=False,
            ):
                st.caption(
                    "Every **source_term → target_column** from `he_en_columns.json` and `he_en_columns_dirty.json` "
                    "under your data root. This is the complete static glossary, independent of SQL log coverage."
                )
                st.dataframe(pd.DataFrame(gs_flat), use_container_width=True, hide_index=True)
        st.markdown(
            "Each row below is either a **log-backed merge** (glossary / exact / vector) or a **glossary file** row "
            "so nothing in `sample_data/glossary/` is hidden. "
            "Sidebar filters (**Business domain**, **Portfolio**) apply to merge-backed rows; **Glossary files** rows "
            "always stay visible when Status = All or **Glossary files**."
        )
        with st.expander("What do these terms mean?", expanded=False):
            st.markdown(
                """
- **Raw mappings** — one row per table × column in the merge output *before* grouping.
- **Unique mappings** — the same logical story after **grouping** identical alignments across tables (fewer rows, easier to read).
- **Names in SQL / logs** — identifiers seen in query text (often Hebrew or legacy spellings).
- **Canonical column (target)** — the name in your **target DDL** / warehouse standard.
- **Merge confidence** — how strong the match is (0–1); glossary and exact DDL hits are typically highest.
- **Status** — **Confirmed** = ready to treat as aligned; **Needs review** = confirm in **Review (HITL)** before production.
"""
            )

        n_gs = int((report.get("glossary_source") or {}).get("total_entries") or 0)
        n_inv = len([x for x in raw_glossary if str(x.get("kind", "")).lower() == "glossary_source"])
        n_merge = len(raw_glossary) - n_inv
        m1, m2, m3 = st.columns(3)
        m1.metric("Raw mappings (merge + glossary file)", len(raw_glossary))
        m2.metric("Unique stories (after grouping)", len(glossary))
        m3.metric("Glossary file rows (sample_data)", n_gs)
        st.caption(
            "Raw counts merge-backed rows plus one row per glossary file entry. "
            "Unique counts one card per distinct alignment (grouped across tables). "
            f"**{n_merge}** from logs, **{n_inv}** from glossary JSON."
        )

        if not raw_glossary:
            st.info(
                "No glossary or merge rows — run **`ama-ingest run`** with glossary JSON under `sample_data/glossary/` "
                "and DDL + SQL logs for full output."
            )
        elif not any(str(x.get("kind", "")).lower() != "glossary_source" for x in raw_glossary) and n_gs:
            st.info(
                "No **log merge** rows yet — expand **Full glossary inventory** above or set Status to **Glossary files** "
                "to browse every static mapping."
            )

        gf1, gf2, gf3 = st.columns([2, 1, 1])
        with gf1:
            gfilter = st.text_input("Search glossary", "", placeholder="term, Hebrew, table name…")
        with gf2:
            kind_pick = st.selectbox(
                "Status",
                options=["All", "Confirmed", "Needs review", "Glossary files"],
                index=0,
                help="Merge status, or only rows loaded from glossary JSON (full sample_data inventory).",
            )
        with gf3:
            sort_pick = st.selectbox(
                "Sort by",
                options=["Business term (A→Z)", "Confidence (high first)"],
                index=0,
            )

        shown = list(glossary)
        if kind_pick == "Confirmed":
            shown = [g for g in shown if str(g.get("kind", "")).lower() == "confirmed"]
        elif kind_pick == "Needs review":
            shown = [g for g in shown if str(g.get("kind", "")).lower() == "review"]
        elif kind_pick == "Glossary files":
            shown = [g for g in shown if str(g.get("kind", "")).lower() == "glossary_source"]

        if gfilter.strip():
            gf = gfilter.strip().lower()
            shown = [
                g
                for g in shown
                if gf in str(g.get("business_term", "")).lower()
                or gf in str(g.get("definition", "")).lower()
                or gf in str(g.get("legacy_columns", "")).lower()
                or gf in str(g.get("domain", "")).lower()
                or gf in " ".join(g.get("source_tables") or []).lower()
            ]

        def _kind_label(k: str) -> str:
            kl = (k or "").lower()
            if kl == "confirmed":
                return "Confirmed"
            if kl == "review":
                return "Needs review"
            if kl == "glossary_source":
                return "Glossary file"
            return k or "—"

        if sort_pick == "Confidence (high first)":
            shown.sort(
                key=lambda x: (
                    -float(x.get("confidence_display", x.get("confidence") or 0.0)),
                    str(x.get("business_term") or ""),
                )
            )
        else:
            shown.sort(
                key=lambda x: (
                    str(x.get("business_term") or ""),
                    str(x.get("target_ddl") or ""),
                )
            )

        summary_rows: list[dict[str, Any]] = []
        for card in shown:
            tables = card.get("source_tables") or ([card.get("source_table")] if card.get("source_table") else [])
            n_tables = len(tables)
            dlist = card.get("domains_list")
            if isinstance(dlist, list) and dlist:
                dom_disp = ", ".join(dlist)
            else:
                dom_disp = str(card.get("domain") or "").strip() or "—"
            primary = (tables[0] if n_tables == 1 else "") or ""
            summary_rows.append(
                {
                    "Business label": card.get("business_term"),
                    "Domain": dom_disp,
                    "Canonical column (target)": card.get("target_ddl"),
                    "Names in SQL / logs": card.get("legacy_columns"),
                    "# Tables": n_tables,
                    "Tables": ", ".join(f"`{t}`" for t in tables if t) or "—",
                    "Primary table": f"`{primary}`" if primary else ("(multiple)" if n_tables > 1 else "—"),
                    "Merge confidence": round(float(card.get("confidence_display", card.get("confidence") or 0.0)), 4),
                    "Status": _kind_label(str(card.get("kind", ""))),
                }
            )
        if summary_rows:
            st.markdown("##### Mapping index")
            st.caption(
                "Sortable overview above. **Mapping cards** below repeat the same rows with full narrative "
                "and confidence — all visible while you scroll (no expand/collapse)."
            )
            st.dataframe(pd.DataFrame(summary_rows), use_container_width=True, hide_index=True)
        elif raw_glossary:
            st.warning("No glossary rows match the current search, status filter, or sidebar filters. Try clearing search or setting Status to **All**.")

        st.markdown("##### Mapping cards")
        st.caption(
            "One card per mapping: definition, legacy→target line, tables, and **merge confidence** — "
            "same order as the index table."
        )
        for i, card in enumerate(shown):
            tables = card.get("source_tables") or ([card.get("source_table")] if card.get("source_table") else [])
            tbl_line = ", ".join(f"`{t}`" for t in tables if t) or "—"
            title_key = f"{card.get('business_term')} → `{card.get('target_ddl')}` · {len(tables)} table(s)"
            sig = hashlib.sha256(
                f"{i}|{title_key}|{card.get('legacy_columns')}|{card.get('kind')}|{','.join(sorted(tables))}".encode(
                    "utf-8"
                )
            ).hexdigest()[:32]
            conf = float(card.get("confidence_display", card.get("confidence") or 0.0))
            dl = card.get("domains_list")
            if isinstance(dl, list) and dl:
                dom_x = ", ".join(dl)
            else:
                dom_x = str(card.get("domain") or "").strip()
            kind_l = _kind_label(str(card.get("kind", "")))

            with _glossary_card_container():
                head_l, head_r = st.columns([2, 1])
                with head_l:
                    st.markdown(f"**{card.get('business_term')}** → `{card.get('target_ddl')}`")
                    st.caption(f"{kind_l} · **{len(tables)}** table(s)" + (f" · **Domain:** {dom_x}" if dom_x else ""))
                with head_r:
                    _confidence_gauge(conf, key=f"gloss_plotly_{i}_{sig}")
                st.markdown(str(card.get("definition", "")))
                st.markdown(
                    f"**Names in SQL / logs** → **Canonical (target):** "
                    f"`{card.get('legacy_columns')}` → **`{card.get('target_ddl')}`**"
                )
                st.markdown(f"**Tables:** {tbl_line}")
                if int(card.get("_group_count") or 1) > 1:
                    st.caption(
                        f"_{int(card.get('_group_count') or 1)} identical merge rows grouped into one view._"
                    )
                if card.get("reasoning"):
                    st.caption(f"**Evidence / citations:** {card.get('reasoning')}")

    with analysis_tabs[7]:
        st.subheader("Ask the data")
        st.markdown(
            """
This is **keyword + synonym search** over structured fields in the report: **discovery inventory** text, 
**merge** rows (confirmed/review), **importance** rows, and **glossary-style** labels. It is **not** full-text 
search over every raw SQL line in the logs.

**Sidebar** (**Business domain**, **Portfolio**) limits which **tables** (and rows tied to those tables) can appear.
"""
        )
        aq = st.text_input(
            "Search business concept",
            placeholder="e.g. amount, כסף, invoice, customer",
            help="Substring match after Hebrew/English synonym expansion (see expander below when you search).",
        )
        if not aq.strip():
            st.info(
                "**Examples to try:** `כסף`, `amount`, `הזמנה`, `invoice`, `customer`, `status`, `revenue`. "
                "Type a term and results show in the three sections below."
            )
        else:
            needles = expand_concept_query(aq)
            with st.expander("Search terms used (including synonyms)", expanded=False):
                st.caption(
                    "These strings are matched as substrings against table/column text (case-insensitive for ASCII)."
                )
                st.write(", ".join(needles) if needles else "—")

            res = semantic_concept_search(report, aq)
            res = filter_semantic_search_results(res, allowed_tables=allowed_tables)
            tabs_out: list[dict[str, Any]] = list(res.get("tables") or [])
            ch: list[dict[str, Any]] = list(res.get("column_hits") or [])
            gh: list[dict[str, Any]] = list(res.get("glossary_hits") or [])

            if not tabs_out and not ch and not gh:
                st.warning(
                    "**No hits** in any section. Widen **Business domain** / set **Portfolio** to **All**, "
                    "try **synonyms** (Hebrew ↔ English, e.g. סכום / amount), or use **shorter keywords**. "
                    "Column hits require matching text in merge or importance rows for tables still in scope."
                )
            else:
                st.caption(
                    "**Column mappings** = flat rows from merge + importance. **Glossary-style matches** = the same "
                    "underlying mappings with business wording — overlap between the two lists is normal."
                )
                if tabs_out:
                    st.markdown("##### Tables (discovery)")
                    ask_tbl_sort = st.selectbox(
                        "Sort table results by",
                        ["Query count (high first)", "Table name (A→Z)"],
                        index=0,
                        key="ask_tbl_sort",
                    )
                    tdf = pd.DataFrame(tabs_out)
                    if ask_tbl_sort == "Query count (high first)" and "queries" in tdf.columns:
                        tdf = tdf.assign(
                            _qask=pd.to_numeric(tdf["queries"], errors="coerce").fillna(0.0)
                        ).sort_values("_qask", ascending=False)
                        tdf = tdf.drop(columns=["_qask"])
                    elif "full_name" in tdf.columns:
                        tdf = tdf.sort_values("full_name", ascending=True)
                    tdf = tdf.rename(
                        columns={
                            "full_name": "Table",
                            "domain": "Domain",
                            "queries": "Query count",
                            "snippet": "Description snippet",
                        }
                    )
                    st.dataframe(tdf, use_container_width=True, hide_index=True)

                st.markdown("##### Column mappings")
                if ch:
                    ch_rows = [
                        {
                            "Match type": _ask_match_type_label(str(h.get("role") or "")),
                            "Source table": h.get("source_table"),
                            "Target column": h.get("ddl"),
                            "Legacy / logs": h.get("legacy"),
                        }
                        for h in ch
                    ]
                    cdf = pd.DataFrame(ch_rows)
                    sort_ask_col = st.selectbox(
                        "Sort column mappings by",
                        ["Match type (A→Z)", "Source table (A→Z)"],
                        index=0,
                        key="ask_col_sort",
                    )
                    if "Source table" in cdf.columns:
                        if sort_ask_col.startswith("Source"):
                            cdf = cdf.sort_values("Source table", ascending=True)
                        else:
                            cdf = cdf.sort_values("Match type", ascending=True)
                    st.dataframe(cdf, use_container_width=True, hide_index=True)
                else:
                    st.caption("No merge or importance rows matched this query for tables in scope.")
                    if tabs_out:
                        st.caption(
                            "Tables above can match on **name**, **domain**, or **description** while column hits need "
                            "the keyword in **canonical**, **legacy**, or **importance** text."
                        )

                st.markdown("##### Glossary-style matches")
                if gh:
                    gh_tab = []
                    for g in gh:
                        if not isinstance(g, dict):
                            continue
                        stbl = g.get("source_table")
                        gh_tab.append(
                            {
                                "Business label": g.get("business_term"),
                                "Canonical column": g.get("target_ddl"),
                                "Domain": g.get("domain") or "—",
                                "Legacy (logs)": g.get("legacy_columns"),
                                "Table": stbl,
                            }
                        )
                    gdf = pd.DataFrame(gh_tab)
                    if not gdf.empty and "Business label" in gdf.columns:
                        gdf = gdf.sort_values("Business label", ascending=True)
                    st.dataframe(gdf, use_container_width=True, hide_index=True)
                else:
                    st.caption(
                        "No glossary-style cards matched. They are built from the same merge output as the **Business Translator** tab — "
                        "try English (`amount`, `price`) or Hebrew (`סכום`) terms."
                    )
                    if tabs_out and not ch:
                        st.caption(
                            "You have **table** hits but no merge-backed glossary yet for this query — often a wording mismatch in column names."
                        )

    with analysis_tabs[4]:
        _ln_l, _ln_r = st.columns([6, 1])
        with _ln_r:
            if st.button("💬 Ask Agent", key="ask_agent_lineage"):
                _set_agent_prefill("Which tables have the most downstream dependencies?")
        st.subheader("Lineage")
        st.caption("Read-only lineage edges from the report JSON.")
        edges = (report.get("lineage") or {}).get("edges") if isinstance(report.get("lineage"), dict) else []
        if isinstance(edges, list) and edges:
            st.dataframe(pd.DataFrame(edges), use_container_width=True, hide_index=True)
        else:
            st.info("No lineage edges found in this report.")

    with analysis_tabs[5]:
        _bk_l, _bk_r = st.columns([6, 1])
        with _bk_r:
            if st.button("💬 Ask Agent", key="ask_agent_bulk"):
                _set_agent_prefill("Help me decide on the right confidence threshold for bulk approval.")
        st.subheader("Bulk Migration")
        st.caption(
            "Approval actions in this tab invoke the Execution layer. Dry run is on by default."
        )
        c1, c2, c3 = st.columns(3)
        c1.slider("Confidence Floor", 50, 100, key="scale_conf_floor")
        c2.slider("Criticality Ceiling", 0, 80, key="scale_crit_ceil")
        c3.selectbox(
            "Target Dialect",
            ["duckdb", "snowflake", "bigquery", "redshift"],
            key="bulk_dialect",
        )
        bulk_workers = st.slider(
            "Bulk Parallel Workers",
            min_value=1,
            max_value=8,
            value=int(st.session_state.get("bulk_workers") or 4),
            step=1,
            key="bulk_workers",
            help="Higher values speed up bulk migration by processing multiple tables concurrently.",
        )
        bulk_dbt_workers = st.slider(
            "Bulk dbt Validation Workers",
            min_value=1,
            max_value=8,
            value=int(st.session_state.get("bulk_dbt_workers") or int(st.session_state.get("bulk_workers") or 4)),
            step=1,
            key="bulk_dbt_workers",
            help="Number of concurrent dbt validation slots during bulk execution.",
        )
        st.session_state["migration_dialect"] = str(st.session_state.get("bulk_dialect") or "duckdb")
        dry_run_bulk = st.toggle("Dry Run", value=True, key="bulk_dry_run")
        requested_workers = int(bulk_workers)
        effective_dbt_workers = int(bulk_dbt_workers)
        st.caption(
            f"Bulk workers: prepare/write={requested_workers}, dbt-validate={effective_dbt_workers}"
        )
        bulk_eval = _get_or_compute_scale_result(
            report,
            int(st.session_state.scale_conf_floor),
            int(st.session_state.scale_crit_ceil),
        )
        if dry_run_bulk:
            st.warning("DRY RUN MODE — no files will be written until you disable dry run.")
        m1, m2, m3, m4 = st.columns(4)
        migrated_tables = set(st.session_state.get("migrated_tables", []))
        greens = [s for s in bulk_eval.scored_tables if s.queue == "green"]
        greens_remaining = [s for s in greens if s.table_key not in migrated_tables]
        yellows = [s for s in bulk_eval.scored_tables if s.queue == "yellow"]
        yellows_remaining = [s for s in yellows if s.table_key not in migrated_tables]
        reds = [s for s in bulk_eval.scored_tables if s.queue == "red"]
        m1.metric("🟢 Ready for Bulk", len(greens_remaining))
        m2.metric("🟡 Review Required", len(yellows_remaining))
        m3.metric("🔴 Blocked", bulk_eval.would_block)
        m4.metric("📋 Contract Rules", len(bulk_eval.contract_preview.rules))

        if greens_remaining:
            if dry_run_bulk:
                if st.button(
                    "🔍 Preview Green Migration (Dry Run)",
                    key="bulk_preview_all_green",
                    type="primary",
                    help=f"See what would happen if you migrated all {len(greens_remaining)} GREEN tables.",
                ):
                    preview = migration_agent_tools.bulk_migrate_tables(
                        report=report,
                        report_path=report_path_resolved or Path("report.json"),
                        filters={"queue": "green"},
                        dialect="duckdb",
                        glossary_path=None,
                        dry_run=True,
                        approved_by="dashboard",
                    )
                    st.info(
                        f"Dry-run summary: migrated={len(preview.migrated)}, skipped={len(preview.skipped)}, "
                        f"contract_id={preview.contract.contract_id if preview.contract else '—'}"
                    )
                    if preview.contract:
                        st.code("\n".join(preview.contract.rules) or "(no rules)")
            else:
                if st.button(
                    "⚡ Approve All Green Tables",
                    key="bulk_approve_all_green",
                    type="primary",
                    help=f"Migrate all {len(greens_remaining)} GREEN tables using the current contract.",
                ):
                    st.session_state["bulk_select_all_green"] = True
                    st.session_state["bulk_run_all_green"] = True
        else:
            st.info(
                "No tables are currently ready for bulk approval. Adjust the confidence threshold or review flagged tables."
            )

        st.markdown("### 🟢 Ready for Bulk")
        gdf = pd.DataFrame(
            [
                {
                    "selected": False,
                    "table_key": s.table_key,
                    "domain": s.business_domain,
                    "confidence": s.confidence,
                    "criticality": s.criticality,
                }
                for s in greens_remaining
            ]
        )
        checked_green: list[str] = []
        if not gdf.empty:
            edited_green = st.data_editor(gdf, use_container_width=True, key="bulk_green_editor", hide_index=True)
            checked_green = [
                str(r["table_key"]) for _, r in edited_green.iterrows() if bool(r.get("selected"))
            ]
        if st.session_state.pop("bulk_select_all_green", False):
            checked_green = [s.table_key for s in greens_remaining]
        if st.button("Preview Contract", key="bulk_preview_contract"):
            st.code("\n".join(bulk_eval.contract_preview.rules) or "(no rules)")
            st.json(
                {
                    "excluded": bulk_eval.contract_preview.excluded,
                    "contract_id": bulk_eval.contract_preview.contract_id,
                }
            )
        if greens_remaining:
            st.caption("Or migrate one GREEN table with explicit approval:")
            for s in greens_remaining[:40]:
                gi1, gi2 = st.columns([5, 1])
                gi1.markdown(f"`{s.table_key}` ({s.business_domain})")
                if gi2.button("▶ Migrate", key=f"bulk_green_migrate_{s.table_key}", type="primary"):
                    if report_path_resolved is None:
                        st.warning("Set a report path first to enable per-table migration.")
                    else:
                        prop = migration_agent_tools.propose_dbt_model(
                            report=report,
                            report_path=report_path_resolved,
                            table=s.table_key,
                            dialect=str(st.session_state.get("migration_dialect") or "duckdb"),
                            glossary_path=None,
                        )
                        pending = _pending_write_from_result(prop)
                        if not isinstance(pending, dict):
                            pending = {
                                "model_name": str(prop.get("model_name") or s.table_key.replace(".", "_")),
                                "sql": str(prop.get("sql") or ""),
                                "schema_yml": str(prop.get("schema_yml") or ""),
                            }
                        st.session_state[f"pending_write_{s.table_key}"] = pending
                        st.rerun()
                _render_pending_write_panel(
                    s.table_key,
                    report_path=report_path_resolved,
                    dbt_project_dir=dashboard_dbt_project_dir,
                    output_dir=dashboard_output_dir,
                    dbt_target=str(st.session_state.get("migration_dialect") or "duckdb"),
                    key_prefix="bulk",
                )
        confirm_bulk = ""
        if not dry_run_bulk:
            confirm_bulk = st.text_input("type CONFIRM to proceed", key="bulk_confirm_text")
        btn_label = "Dry Run Selected" if dry_run_bulk else "Approve Selected"
        run_selected = st.button(btn_label, key="bulk_approve_selected", disabled=not checked_green)
        run_all_now = bool(st.session_state.pop("bulk_run_all_green", False))
        bulk_job_id = str(st.session_state.get("bulk_job_id") or "")
        bulk_job = None
        if bulk_job_id:
            with _BULK_JOBS_LOCK:
                bulk_job = _BULK_JOBS.get(bulk_job_id)
            if not isinstance(bulk_job, dict):
                bulk_job = _bulk_job_load(
                    dbt_project_dir=dashboard_dbt_project_dir,
                    job_id=bulk_job_id,
                )
        if bulk_job_id and not isinstance(bulk_job, dict):
            st.info("No active bulk job state found.")
            if st.button("Clear Bulk Status", key="bulk_job_clear_stale"):
                st.session_state["bulk_job_id"] = ""
                st.rerun()
        if isinstance(bulk_job, dict):
            status_txt = str(bulk_job.get("status") or "")
            completed = int(bulk_job.get("completed") or 0)
            total = int(bulk_job.get("total") or 0)
            current = str(bulk_job.get("current_table") or "")
            prep_workers = int(bulk_job.get("workers") or requested_workers)
            dbt_w = int(bulk_job.get("dbt_workers") or effective_dbt_workers)
            st.info(
                f"Bulk job running: {completed}/{total} (status={status_txt or 'running'})"
                + (f" — current `{current}`" if current else "")
                + f" — workers prep={prep_workers}, dbt={dbt_w}"
            )
            st.progress((completed / max(1, total)) if total > 0 else 0.0)
            # Keep the UI live while background job advances.
            if status_txt in {"queued", "running"}:
                st.caption("Auto-refreshing bulk job status...")
                time.sleep(1.2)
                st.rerun()
            if status_txt == "failed":
                st.error(f"Bulk migration failed: {str(bulk_job.get('error') or 'unknown error')}")
            if status_txt == "done":
                success_tables = list(bulk_job.get("success") or [])
                failed_rows = list(bulk_job.get("failed") or [])
                failed_tables = [
                    str(r.get("table_key") if isinstance(r, dict) else r)
                    for r in failed_rows
                ]
                if success_tables:
                    migrated = set(st.session_state.get("migrated_tables", []))
                    migrated.update(success_tables)
                    st.session_state["migrated_tables"] = sorted(migrated)
                    st.session_state[MIGRATION_NOTICE_KEY] = (
                        f"Bulk migration finished: {len(success_tables)} table(s) marked as migrated."
                    )
                st.success(f"✅ {len(success_tables)} tables migrated successfully.")
                if failed_rows:
                    st.warning(f"⚠️ {len(failed_rows)} tables need review.")
                    with st.expander("Show failed tables and reasons", expanded=False):
                        for row in failed_rows:
                            if isinstance(row, dict):
                                st.markdown(
                                    f"- `{str(row.get('table_key') or '')}`: {str(row.get('reason') or 'failed')}"
                                )
                            else:
                                st.markdown(f"- `{str(row)}`")
                if st.button("Clear Bulk Job", key="bulk_job_clear"):
                    _bulk_job_clear(dbt_project_dir=dashboard_dbt_project_dir, job_id=bulk_job_id)
                    st.session_state["bulk_job_id"] = ""
                    st.rerun()
        if run_selected or run_all_now:
            if dry_run_bulk:
                st.info(f"Dry-run only: {len(checked_green)} table(s) selected.")
            elif confirm_bulk.strip() == "CONFIRM":
                if bulk_job and str(bulk_job.get("status") or "") == "running":
                    st.warning("A bulk migration job is already running.")
                else:
                    row_by_key = {s.table_key: s for s in greens_remaining}
                    if not checked_green:
                        st.warning("No GREEN tables selected for bulk migration.")
                    else:
                        scored_rows: dict[str, Any] = {}
                        for k in checked_green:
                            s = row_by_key.get(k)
                            if s is None:
                                continue
                            scored_rows[k] = {
                                "queue": s.queue,
                                "confidence_result": s.confidence_result,
                                "criticality_result": s.criticality_result,
                                "anomaly_flags": s.anomaly_flags,
                            }
                        new_job_id = str(uuid.uuid4())
                        st.session_state["bulk_job_id"] = new_job_id
                        with _BULK_JOBS_LOCK:
                            _BULK_JOBS[new_job_id] = {
                                "status": "queued",
                                "total": len(checked_green),
                                "completed": 0,
                                "current_table": "",
                                "success": [],
                                "failed": [],
                                "error": "",
                            }
                            _bulk_job_write(
                                dbt_project_dir=dashboard_dbt_project_dir,
                                job_id=new_job_id,
                                payload=dict(_BULK_JOBS[new_job_id]),
                            )
                        th = threading.Thread(
                            target=_run_bulk_job,
                            kwargs={
                                "job_id": new_job_id,
                                "table_keys": list(checked_green),
                                "report": report,
                                "report_path": report_path_resolved or Path("report.json"),
                                "dialect": str(st.session_state.get("migration_dialect") or "duckdb"),
                                "dbt_project_dir": dashboard_dbt_project_dir,
                                "output_dir": dashboard_output_dir,
                                "contract_id": bulk_eval.contract_preview.contract_id,
                                "scored_rows": scored_rows,
                                "max_workers": int(bulk_workers),
                                "dbt_workers": int(effective_dbt_workers),
                                "dbt_target": str(st.session_state.get("migration_dialect") or "duckdb"),
                            },
                            daemon=True,
                        )
                        th.start()
                        st.success("Bulk migration started in background. You can keep reviewing other tables.")
                        st.rerun()
            else:
                st.warning("Type CONFIRM first, then run bulk approval.")

        st.markdown("### 🟡 Review Required")
        for s in yellows_remaining:
            warn_reason = next((f.reason for f in s.anomaly_flags if f.level == "WARN"), s.confidence_result.reason)
            with st.expander(
                f"{s.table_key} — primary: {warn_reason[:120]}",
                expanded=False,
            ):
                st.write("**Confidence**", s.confidence_result)
                st.write("**Criticality**", s.criticality_result)
                for f in s.anomaly_flags:
                    st.write(f"`{f.level}` **{f.name}**: {f.reason}")
                if st.button("Approve Individually", key=f"bulk_ind_{s.table_key}"):
                    st.info("Use **Migration Agent** or **HITL Review** for per-table approval with write gate.")

        st.markdown("### 🔴 Blocked")
        st.caption("These tables require manual architectural review.")
        for s in reds:
            cols_blk = st.columns([4, 1])
            block_reason = next(
                (f.reason for f in s.anomaly_flags if f.level == "BLOCK"),
                s.criticality_result.reason,
            )
            cols_blk[0].write(f"{s.table_key} ({s.business_domain}) — {block_reason[:160]}")
            if cols_blk[1].button("Explain", key=f"bulk_exp_{s.table_key}"):
                st.session_state[f"bulk_explain_{s.table_key}"] = migration_agent_tools.explain_table_score(
                    report=report,
                    table_key=s.table_key,
                )
            _explain_key = f"bulk_explain_{s.table_key}"
            if _explain_key in st.session_state:
                ex = st.session_state[_explain_key]
                with st.expander(f"Explanation — {s.table_key}", expanded=True):
                    st.markdown(f"**Queue:** {ex.queue}")
                    st.markdown(
                        f"**Migration Confidence:** {ex.confidence.score} — {ex.confidence.reason}"
                    )
                    st.markdown(
                        f"**Criticality:** {ex.criticality.score} — {ex.criticality.reason}"
                    )
                    if ex.anomaly_flags:
                        st.markdown("**Anomaly Flags:**")
                        for flag in ex.anomaly_flags:
                            st.markdown(f"- `{flag.level}` **{flag.name}**: {flag.reason}")
                    st.markdown(f"**Summary:** {ex.summary}")

        with st.expander("Audit Log Viewer", expanded=False):
            from ama.config import project_root

            audit_path = project_root() / "audit_trail.jsonl"
            audit_rows: list[dict[str, Any]] = []
            if audit_path.is_file():
                for line in audit_path.read_text(encoding="utf-8").splitlines()[-50:]:
                    try:
                        audit_rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
            if audit_rows:
                cols_use = [
                    c
                    for c in (
                        "timestamp",
                        "table_key",
                        "decision",
                        "contract_id",
                        "approved_by",
                        "primary_reason",
                    )
                    if c in audit_rows[0]
                ]
                st.dataframe(pd.DataFrame(audit_rows)[cols_use], use_container_width=True, hide_index=True)
                st.download_button(
                    "Download Full Audit Trail",
                    data=audit_path.read_text(encoding="utf-8"),
                    file_name="audit_trail.jsonl",
                    mime="application/json",
                )
            else:
                st.caption("No audit entries yet.")

    with analysis_tabs[2]:
        st.subheader("Tables")
        if bool(_bulk_applied_now):
            st.rerun()
        if isinstance(_bulk_job_state, dict):
            _bulk_status = str(_bulk_job_state.get("status") or "")
            if _bulk_status in {"queued", "running"}:
                _done = int(_bulk_job_state.get("completed") or 0)
                _tot = int(_bulk_job_state.get("total") or 0)
                _cur = str(_bulk_job_state.get("current_table") or "")
                st.info(
                    f"Bulk job running: {_done}/{_tot}"
                    + (f" — current `{_cur}`" if _cur else "")
                )
                st.progress((_done / max(1, _tot)) if _tot > 0 else 0.0)
                st.caption("Auto-refreshing bulk job status...")
                time.sleep(1.2)
                st.rerun()
        st.caption(
            "Row level → **Migration Confidence** (Scale Engine, per-table). "
            "Field level → **Merge Confidence** (alias resolver, per-column)."
        )
        _scale_result = _get_or_compute_scale_result(
            report,
            int(st.session_state.scale_conf_floor),
            int(st.session_state.scale_crit_ceil),
        )
        scored_by_table = {s.table_key: s for s in _scale_result.scored_tables}
        _tb_l, _tb_r = st.columns([6, 1])
        with _tb_r:
            if st.button("💬 Ask Agent", key="ask_agent_tables"):
                _pick = str(st.session_state.get("tbl_pick_main") or "").strip()
                if _pick and _pick in scored_by_table:
                    _queue = scored_by_table[_pick].queue
                    _set_agent_prefill(
                        f"Why is {_pick} scored as {_queue}? What should I do next?"
                    )
                else:
                    _set_agent_prefill("Show me all tables ready for bulk migration.")
        migrated_tables = set(st.session_state.get("migrated_tables", []))
        green_count = sum(
            1
            for s in _scale_result.scored_tables
            if s.queue == "green" and str(s.table_key) not in migrated_tables
        )
        if green_count > 0:
            _c_msg, _c_btn = st.columns([5, 1])
            _c_msg.success(
                f"🟢 {green_count} tables are ready for bulk approval. Approve them all at once in the Bulk Migration tab."
            )
            if _c_btn.button("Go to Bulk Migration →", key="tables_go_bulk"):
                st.session_state["analysis_focus_tab"] = "Bulk Migration"
        tbl_max_conf = _table_max_merge_confidence(merged_all, review_all, trash_all)
        tq1, tq2 = st.columns([2, 1])
        with tq1:
            q = st.text_input("Search tables / schemas", "", key="tbl_q")
        with tq2:
            tbl_list_sort = st.selectbox(
                "Sort tables by",
                [
                    "Table name (A→Z)",
                    "Query volume (high first)",
                    "Max merge confidence (high first)",
                ],
                index=0,
                key="tbl_list_sort",
                help="Order of rows in the inventory table and in **Select a table** below.",
            )
        show = inv_view.copy()
        if not show.empty and "full_name" in show.columns:
            show["Migration Confidence"] = show["full_name"].map(
                lambda fn: int(scored_by_table[str(fn)].confidence) if str(fn) in scored_by_table else 0
            )
            show["Criticality"] = show["full_name"].map(
                lambda fn: int(scored_by_table[str(fn)].criticality) if str(fn) in scored_by_table else 0
            )
            show["Queue"] = show["full_name"].map(
                lambda fn: queue_emoji(scored_by_table[str(fn)].queue) if str(fn) in scored_by_table else "🔴 Blocked"
            )
            show["Anomaly Flags"] = show["full_name"].map(
                lambda fn: ", ".join(f.name for f in scored_by_table[str(fn)].anomaly_flags)
                if str(fn) in scored_by_table
                else ""
            )
        qf_col1, qf_col2, qf_col3, qf_col4 = st.columns(4)
        queue_filter = qf_col1.multiselect(
            "Queue filter",
            options=["🟢 Bulk", "🟡 Review", "🔴 Blocked"],
            default=["🟢 Bulk", "🟡 Review", "🔴 Blocked"],
            key="tbl_queue_filter",
        )
        min_conf_tbl = qf_col2.slider("Min Confidence", 0, 100, 0, key="tbl_min_conf")
        max_crit_tbl = qf_col3.slider("Max Criticality", 0, 100, 100, key="tbl_max_crit")
        anomaly_filter_tbl = qf_col4.multiselect(
            "Anomaly flag filter",
            options=["BLOCK", "WARN", "INFO", "None"],
            default=["BLOCK", "WARN", "INFO", "None"],
            key="tbl_anom_filter",
        )
        if not show.empty and "Queue" in show.columns:
            show = show[show["Queue"].isin(queue_filter)]
            show = show[show["Migration Confidence"] >= min_conf_tbl]
            show = show[show["Criticality"] <= max_crit_tbl]
            if set(anomaly_filter_tbl) != {"BLOCK", "WARN", "INFO", "None"}:
                selected_lvls = set(anomaly_filter_tbl)
                keep_rows: list[int] = []
                for idx, row in show.reset_index(drop=True).iterrows():
                    fn = str(row.get("full_name") or "")
                    flags = scored_by_table[fn].anomaly_flags if fn in scored_by_table else []
                    levels = {f.level for f in flags}
                    if "None" in selected_lvls and not levels:
                        keep_rows.append(idx)
                    elif levels.intersection(selected_lvls):
                        keep_rows.append(idx)
                show = show.reset_index(drop=True).iloc[keep_rows] if keep_rows else show.iloc[[]]
        if q.strip() and not show.empty:
            mask = show.astype(str).apply(lambda s: s.str.contains(q, case=False, na=False)).any(axis=1)
            show = show[mask]

        if not show.empty and "full_name" in show.columns:
            if tbl_list_sort == "Query volume (high first)" and "query_count" in show.columns:
                show = (
                    show.assign(
                        _qvol=pd.to_numeric(show["query_count"], errors="coerce").fillna(0.0)
                    )
                    .sort_values("_qvol", ascending=False)
                    .drop(columns=["_qvol"])
                )
            elif tbl_list_sort == "Max merge confidence (high first)":
                show = show.copy()
                show["_tmc"] = show["full_name"].map(lambda fn: tbl_max_conf.get(str(fn), -1.0))
                show = show.sort_values("_tmc", ascending=False).drop(columns=["_tmc"])
            elif tbl_list_sort == "Table name (A→Z)":
                show = show.sort_values("full_name", ascending=True)

        TBL_PICK_KEY = "tbl_pick_main"
        df_view: pd.DataFrame | None = None
        if not show.empty and "full_name" in show.columns:
            show = show.copy()
            show["_risk"] = show["full_name"].map(lambda fn: "High lineage impact" if fn in risk_set else "")
            display_cols = [c for c in show.columns if c != "_risk"]
            df_view = show[display_cols + ["_risk"]].rename(columns={"_risk": "Risk"})

        tables = (
            show["full_name"].dropna().astype(str).unique().tolist()
            if not show.empty and "full_name" in show.columns
            else []
        )
        valid_tbl = set(tables)
        if TBL_PICK_KEY in st.session_state:
            pv = st.session_state[TBL_PICK_KEY]
            if pv and valid_tbl and str(pv) not in valid_tbl:
                st.session_state[TBL_PICK_KEY] = ""

        if df_view is not None:
            st.caption("**Click a row** in the table below to select that table (updates **Select a table**).")
            try:
                grid_state = st.dataframe(
                    df_view,
                    use_container_width=True,
                    hide_index=True,
                    on_select="rerun",
                    selection_mode="single-row",
                    key="tbl_inv_grid",
                )
                _sync_tbl_pick_from_dataframe(
                    grid_state,
                    df_view,
                    valid_tables=valid_tbl,
                    session_key=TBL_PICK_KEY,
                )
            except TypeError:
                st.dataframe(df_view, use_container_width=True, hide_index=True)
        else:
            st.dataframe(show, use_container_width=True, hide_index=True)
            if show.empty:
                if q.strip() and not inv_view.empty:
                    st.caption(
                        "No rows match the search box; clear the search to see all tables in scope."
                    )
                elif not q.strip() and inv_view.empty:
                    st.caption("No inventory rows match domain/portfolio filters.")

        if tables:
            st.markdown("### Table Actions")
            st.caption("Individual migration path: act per table based on its queue.")
            migrated_tables = set(st.session_state.get("migrated_tables", []))
            done_msg = str(st.session_state.get(MIGRATION_NOTICE_KEY) or "").strip()
            if done_msg:
                st.success(done_msg)
            hide_migrated = st.checkbox("Hide migrated tables", value=True, key="hide_migrated_tables")
            table_rows = [t for t in tables if not (hide_migrated and str(t) in migrated_tables)]
            if hide_migrated and len(table_rows) < len(tables):
                st.caption(
                    f"Hidden migrated tables: {len(tables) - len(table_rows)}. "
                    "Uncheck to show all."
                )
            for t in table_rows[:80]:
                scored = scored_by_table.get(str(t))
                if scored is None:
                    continue
                lcol, rcol = st.columns([5, 1])
                lcol.write(f"`{t}` — {queue_emoji(scored.queue)}")
                if str(t) in migrated_tables:
                    rcol.success("✅ Done")
                elif scored.queue == "green":
                    if rcol.button("▶ Migrate", key=f"tbl_migrate_{t}", type="primary"):
                        if report_path_resolved is None:
                            st.warning("Set a report path first to enable per-table migration.")
                        else:
                            prop = migration_agent_tools.propose_dbt_model(
                                report=report,
                                report_path=report_path_resolved,
                                table=str(t),
                                dialect=str(st.session_state.get("migration_dialect") or "duckdb"),
                                glossary_path=None,
                            )
                            pending = _pending_write_from_result(prop)
                            if not isinstance(pending, dict):
                                pending = {
                                    "model_name": str(prop.get("model_name") or str(t).replace(".", "_")),
                                    "sql": str(prop.get("sql") or ""),
                                    "schema_yml": str(prop.get("schema_yml") or ""),
                                }
                            st.session_state[f"pending_write_{t}"] = pending
                            st.rerun()
                    _render_pending_write_panel(
                        str(t),
                        report_path=report_path_resolved,
                        dbt_project_dir=dashboard_dbt_project_dir,
                        output_dir=dashboard_output_dir,
                        dbt_target=str(st.session_state.get("migration_dialect") or "duckdb"),
                        key_prefix="tables",
                    )
                elif scored.queue == "yellow":
                    if rcol.button(
                        "👁 Review",
                        key=f"tbl_review_{t}",
                        help="Medium confidence — review required before migrating.",
                    ):
                        st.session_state[f"tbl_review_open_{t}"] = True
                    if st.session_state.get(f"tbl_review_open_{t}"):
                        with st.expander(f"Review Details — {t}", expanded=True):
                            st.markdown(
                                f"**Migration Confidence:** {scored.confidence_result.score} — {scored.confidence_result.reason}"
                            )
                            st.markdown(
                                f"**Criticality:** {scored.criticality_result.score} — {scored.criticality_result.reason}"
                            )
                            for flag in scored.anomaly_flags:
                                st.markdown(f"- `{flag.level}` **{flag.name}**: {flag.reason}")
                            if st.button(
                                "▶ Migrate (after review)",
                                key=f"tbl_migrate_yellow_{t}",
                                type="primary",
                            ):
                                if _queue_table_pending_write(
                                    table_key=str(t),
                                    report=report,
                                    report_path=report_path_resolved,
                                    dialect=str(st.session_state.get("migration_dialect") or "duckdb"),
                                ):
                                    st.rerun()
                    _render_pending_write_panel(
                        str(t),
                        report_path=report_path_resolved,
                        dbt_project_dir=dashboard_dbt_project_dir,
                        output_dir=dashboard_output_dir,
                        dbt_target=str(st.session_state.get("migration_dialect") or "duckdb"),
                        key_prefix="tables",
                    )
                else:
                    if rcol.button(
                        "🔍 Explain",
                        key=f"tbl_explain_{t}",
                        help="Blocked — see explanation for details.",
                    ):
                        st.session_state[f"tbl_explain_result_{t}"] = migration_agent_tools.explain_table_score(
                            report=report,
                            table_key=str(t),
                        )
                    if f"tbl_explain_result_{t}" in st.session_state:
                        ex = st.session_state[f"tbl_explain_result_{t}"]
                        with st.expander(f"Explanation — {t}", expanded=True):
                            st.markdown(f"**Queue:** {ex.queue}")
                            st.markdown(f"**Migration Confidence:** {ex.confidence.score} — {ex.confidence.reason}")
                            st.markdown(f"**Criticality:** {ex.criticality.score} — {ex.criticality.reason}")
                            if ex.anomaly_flags:
                                st.markdown("**Anomaly Flags:**")
                                for flag in ex.anomaly_flags:
                                    st.markdown(f"- `{flag.level}` **{flag.name}**: {flag.reason}")
                            st.markdown(f"**Summary:** {ex.summary}")

        with st.expander("Select a table", expanded=bool(tables)):
            pick = st.selectbox(
                "Choose from list (or click a row above)",
                options=[""] + tables,
                key=TBL_PICK_KEY,
                help="Synced when you select a row in the inventory table.",
            )

        if pick:
            st.markdown(f"#### `{pick}`")
            scored_pick = scored_by_table.get(pick)
            if scored_pick is not None:
                st.write(
                    f"**Migration Confidence:** {scored_pick.confidence} · **Criticality:** {scored_pick.criticality} · "
                    f"**Queue:** {queue_emoji(scored_pick.queue)}"
                )
                st.caption(f"Conf. Reason — {scored_pick.confidence_result.reason}")
                st.caption(f"Crit. Reason — {scored_pick.criticality_result.reason}")
                if scored_pick.anomaly_flags:
                    for flag in scored_pick.anomaly_flags:
                        st.markdown(f"- `{flag.level}` **{flag.name}**: {flag.reason}")
                else:
                    st.caption("No anomaly flags.")
            map_sort = st.selectbox(
                "Sort mappings by",
                ["Confidence (high first)", "Target column (A→Z)"],
                index=0,
                key="tbl_map_sort",
                help="Applies to Confirmed, Review, and Trash rows for the selected table.",
            )
            st.caption("Field-level **Merge Confidence** is shown in the column mapping tables below.")
            dom = _domain_for_table(report, pick)
            if dom:
                st.write(f"**Domain:** {dom}")
            fs = None
            for row in (exec_sum.get("table_fact_sheets") or []):
                if isinstance(row, dict) and str(row.get("full_qualified_name")) == pick:
                    fs = row.get("business_description")
                    break
            if fs:
                st.info(fs)

            lineage_block = report.get("lineage") or {}
            broken_tbl = broken_tables_from_report(report)
            lg_html = lineage_subgraph_html(lineage_block, pick, broken_tables=broken_tbl)
            has_edges = bool(lineage_block.get("edges"))
            if lg_html:
                with st.expander("Lineage Graph (Interactive)", expanded=True):
                    components.html(lg_html, height=480, scrolling=True)
            elif has_edges:
                with st.expander("Lineage Graph (Interactive)", expanded=True):
                    if pyvis_available():
                        st.caption("No neighborhood edges for this table in the current graph.")
                    else:
                        st.warning(PYVIS_INSTALL_HINT)
            else:
                st.caption("No lineage edges for this report (run with `--discovery-mode` to build the graph).")

            rh_row = next(
                (
                    x
                    for x in rh_exec_filtered
                    if isinstance(x, dict) and str(x.get("table")) == pick
                ),
                None,
            )
            if rh_row:
                st.markdown("**Blast radius (lineage)**")
                st.write(
                    f"Score **{rh_row.get('blast_radius_score', '')}** — "
                    f"domains: {', '.join(rh_row.get('domains_touched') or [])} — "
                    f"reach: **{rh_row.get('downstream_tables_reached', '')}** tables"
                )

            me = [e for e in merged_all if str(e.get("source_table")) == pick]
            rev = [e for e in review_all if str(e.get("source_table")) == pick]
            if map_sort == "Confidence (high first)":
                me = sorted(me, key=_merge_conf_float, reverse=True)
                rev = sorted(rev, key=_merge_conf_float, reverse=True)
            else:
                me = sorted(me, key=lambda e: str((e or {}).get("canonical_column") or "").lower())
                rev = sorted(
                    rev,
                    key=lambda e: str((e or {}).get("suggested_ddl") or (e or {}).get("legacy_name") or "").lower(),
                )
            st.markdown("**Confirmed → DDL**")
            if me:
                st.dataframe(
                    pd.DataFrame(
                        [
                            {
                                "DDL": e.get("canonical_column"),
                                "Merge Confidence": e.get("merge_confidence"),
                                "Strategy": ",".join(e.get("strategies") or []) if e.get("strategies") else "",
                                "Legacy sources": ", ".join(e.get("source_columns") or []),
                            }
                            for e in me
                        ]
                    ),
                    use_container_width=True,
                    hide_index=True,
                )
            else:
                st.caption("No confirmed mappings for this table in scope.")

            st.markdown("**Review**")
            if rev:
                st.dataframe(
                    pd.DataFrame(
                        [
                            {
                                "Legacy": e.get("legacy_name"),
                                "Suggested DDL": e.get("suggested_ddl"),
                                "Merge Confidence": e.get("merge_confidence"),
                            }
                            for e in rev
                        ]
                    ),
                    use_container_width=True,
                    hide_index=True,
                )
            else:
                st.caption("No review rows for this table.")

            tr = [e for e in trash_all if str(e.get("source_table")) == pick]
            if map_sort == "Confidence (high first)":
                tr = sorted(tr, key=_merge_conf_float, reverse=True)
            else:
                tr = sorted(
                    tr,
                    key=lambda e: str(
                        (e or {}).get("legacy_name")
                        or (e or {}).get("canonical_column")
                        or (e or {}).get("suggested_ddl")
                        or ""
                    ).lower(),
                )
            with st.expander("Trash / low-signal", expanded=True):
                if tr:
                    st.dataframe(pd.DataFrame(tr), use_container_width=True, hide_index=True)
                else:
                    st.caption("No trash rows for this table.")

    with analysis_tabs[8]:
        _render_dq_tab(report)

    with execution_tabs[1]:
        st.subheader("Human-in-the-loop — review queue")
        st.caption("Approve or reject suggested mappings. Decisions persist next to the JSON report.")
        decisions = st.session_state.hitl.setdefault("decisions", {})

        for i, row in enumerate(review_all):
            if not isinstance(row, dict):
                continue
            sig = review_row_signature(row)
            prior = decisions.get(sig, {})
            with st.expander(
                f"`{row.get('legacy_name')}` → `{row.get('suggested_ddl')}` @ {row.get('source_table')}",
                expanded=False,
            ):
                st.json(row)
                c1, c2, c3 = st.columns(3)
                if c1.button("Approve", key=f"ap_{sig}_{i}"):
                    decisions[sig] = {
                        "action": "approved",
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "row": {
                            "source_table": row.get("source_table"),
                            "legacy_name": row.get("legacy_name"),
                            "suggested_ddl": row.get("suggested_ddl"),
                        },
                    }
                    st.session_state.hitl["decisions"] = decisions
                    if hitl_file:
                        _save_hitl(hitl_file, st.session_state.hitl)
                    st.success("Saved: approved — refreshing KPIs…")
                    st.rerun()
                if c2.button("Reject", key=f"rj_{sig}_{i}"):
                    decisions[sig] = {
                        "action": "rejected",
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "row": {
                            "source_table": row.get("source_table"),
                            "legacy_name": row.get("legacy_name"),
                            "suggested_ddl": row.get("suggested_ddl"),
                        },
                    }
                    st.session_state.hitl["decisions"] = decisions
                    if hitl_file:
                        _save_hitl(hitl_file, st.session_state.hitl)
                    st.warning("Saved: rejected — refreshing…")
                    st.rerun()
                status = prior.get("action", "—")
                c3.caption(f"Last decision: **{status}**")

        n_raw_review = len((am.get("review_candidates") or []))
        if not review_all:
            if n_raw_review == 0:
                st.warning(
                    "**This report has no `review_candidates`.** "
                    "The merge step only lists **medium-confidence** unmapped columns here; "
                    "many real runs classify almost everything as **confirmed** or **trash**, so this array is often empty."
                )
                st.markdown(
                    f"- **Try the demo report** (two sample review rows): `{_DEMO_WITH_REVIEW}`  \n"
                    "- **Regenerate** with a higher bar for auto-confirm, e.g.  \n"
                    "  `ama-ingest run ... --confirmed-threshold 0.95`  \n"
                    "  so borderline matches stay in **review** instead of **confirmed**."
                )
            else:
                st.warning(
                    f"The file contains **{n_raw_review}** review row(s), but **sidebar filters** "
                    "hide them all. Clear **Business domain** selections and set **Portfolio** to **All**."
                )

        if hitl_file:
            st.caption(
                f"Sidecar path: `{hitl_file}` — the file is **created** when you first click "
                "**Approve** or **Reject** (it does not exist until then)."
            )
        else:
            st.caption("Upload mode: decisions stay in session only. Use a file path to persist.")
            st.download_button(
                "Download HITL JSON",
                data=json.dumps(st.session_state.hitl, indent=2, ensure_ascii=False),
                file_name="report.hitl.json",
                mime="application/json",
            )

        st.divider()
        if report_path_resolved is not None:
            st.subheader("Submit to report JSON (apply decisions)")
            confirm_key = "hitl_submit_confirm"
            if st.button("Submit HITL decisions to disk", key="hitl_submit_btn"):
                st.session_state[confirm_key] = True
            if st.session_state.get(confirm_key):
                if st.button("Confirm Submit (overwrite report.json)", key="hitl_submit_confirm_go"):
                    merged = apply_hitl_to_report(raw_report, st.session_state.hitl)
                    report_path_resolved.write_text(
                        json.dumps(merged, indent=2, ensure_ascii=False),
                        encoding="utf-8",
                    )
                    st.session_state[confirm_key] = False
                    # Refresh cached report
                    st.session_state.report_reload_bust = int(st.session_state.report_reload_bust) + 1
                    load_report_cached.clear()
                    st.success("Submitted: overwrote report.json and refreshed dashboard.")
                    st.rerun()
        else:
            st.caption("Submit is disabled when no local `report_path_resolved` exists (upload mode).")

    with execution_tabs[0]:
        _render_migration_agent_tab(report)

    st.divider()
    st.caption(
        "Sidebar filters (**Business domain**, **Portfolio**) apply across Executive overview, Domains, "
        "Business Glossary, Ask the data, Tables, and HITL. **Merge confidence** stays visual (scatter, gauges, columns)."
    )


if __name__ == "__main__":
    main()
