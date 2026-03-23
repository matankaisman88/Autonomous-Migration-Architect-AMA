"""
AMA Streamlit dashboard: Business Translator, domain deep dives, search, HITL.

Loads the same JSON as Excel (`ama-ingest run --format json`). Optional sidecar
`<report>.hitl.json` stores approve/reject decisions for review rows.
"""

from __future__ import annotations

import hashlib
import json
import os
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
    dq = run_dq_suite(report)
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


def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


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

    default_path = os.environ.get("AMA_REPORT_PATH", "").strip()
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
    st.caption(
        "Sidebar filters (**Business domain**, **Portfolio**) apply across Executive overview, Domains, "
        "Business Glossary, Ask the data, Tables, and HITL. **Merge confidence** stays visual (scatter, gauges, columns)."
    )


if __name__ == "__main__":
    main()
