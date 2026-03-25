"""
AMA Streamlit dashboard: Business Translator, domain deep dives, search, HITL.

Loads the same JSON as Excel (`ama-ingest run --format json`). Optional sidecar
`<report>.hitl.json` stores approve/reject decisions for review rows.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import threading
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
from ama.dbt_migration.writer import write_model_artifacts
from ama.dbt_migration.runner import (
    execute_models_with_fix_loop,
    approve_checkpoint_b_sql,
    reject_checkpoint_b_to_dlq,
)
from ama.dbt_migration.service import run_generate_dbt
from ama.dbt_migration.service import start_generate_dbt_checkpoint_a_job, poll_generate_dbt_checkpoint_a_job
from ama.dbt_migration.service import apply_ai_fix_from_checkpoint
from ama.dbt_migration.service import (
    analyze_model_risk_and_scenarios,
    generate_synthetic_data_for_model,
    propose_sql_patch_from_chat,
    run_wave_stress_test,
)
from ama.dbt_migration.models import (
    MigrationSessionState,
    MigrationStatus,
    ModelRunState,
    RunnerFinalStatus,
)
from ama.env_resolver import get_env, get_openai_model

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
            wave_pick = st.selectbox(
                "Selective Generation (dbt Migration tab)",
                options=[None] + wave_ids_sorted,
                format_func=lambda x: "—" if x is None else f"Wave {x}",
                index=0,
                key="planner_wave_pick",
            )
            if st.button("Queue into dbt Migration tab", key="planner_queue_wave"):
                st.session_state.setdefault("dbt_migration", {})
                st.session_state["dbt_migration"]["selected_wave_id"] = wave_pick
                st.success("Queued wave selection. Open the `dbt Migration` tab to continue.")
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

    tabs = st.tabs(
        [
            "Executive overview",
            "Domains",
            "Planner",
            "Business Glossary",
            "Ask the data",
            "Tables",
            "Data quality",
            "Review (HITL)",
            "dbt Migration",
        ]
    )

    with tabs[0]:
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

    with tabs[1]:
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

    with tabs[2]:
        _render_planner_tab(report)

    with tabs[3]:
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

    with tabs[4]:
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

    with tabs[5]:
        st.subheader("Tables")
        st.caption(
            "Table list follows **business domain** and **portfolio** (discovery inventory). "
            "Confirmed / Review / Trash show **merge_confidence** per row in the **Confidence** column — compare mappings at a glance."
        )
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
        show = inv_view
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

        with st.expander("Select a table", expanded=bool(tables)):
            pick = st.selectbox(
                "Choose from list (or click a row above)",
                options=[""] + tables,
                key=TBL_PICK_KEY,
                help="Synced when you select a row in the inventory table.",
            )

        if pick:
            st.markdown(f"#### `{pick}`")
            map_sort = st.selectbox(
                "Sort mappings by",
                ["Confidence (high first)", "Target column (A→Z)"],
                index=0,
                key="tbl_map_sort",
                help="Applies to Confirmed, Review, and Trash rows for the selected table.",
            )
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
                                "Confidence": e.get("merge_confidence"),
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
                                "Confidence": e.get("merge_confidence"),
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

    with tabs[6]:
        _render_dq_tab(report)

    with tabs[7]:
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
                st.write(row)
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

    with tabs[8]:
        _render_dbt_migration_tab(report)

    st.divider()
    st.caption(
        "Sidebar filters (**Business domain**, **Portfolio**) apply across Executive overview, Domains, "
        "Business Glossary, Ask the data, Tables, and HITL. **Merge confidence** stays visual (scatter, gauges, columns)."
    )


if __name__ == "__main__":
    main()
