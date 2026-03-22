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

from ama.business_logic import (
    build_business_glossary_entries,
    build_impact_readiness_scatter_rows,
    domain_data_health,
    group_glossary_entries,
    review_row_signature,
    semantic_concept_search,
)
from ama.ui.report_helpers import (
    _domain_for_table,
    _high_risk_tables,
    _inventory_df,
    _merge_rows_for_filters,
    _pct_confirmed,
    load_report_json,
)

try:
    import plotly.express as px
    import plotly.graph_objects as go
except ImportError:  # pragma: no cover
    px = None  # type: ignore[assignment]
    go = None  # type: ignore[assignment]

# Repo root: src/ama/ui/dashboard.py -> parents[3]
_DEMO_WITH_REVIEW = Path(__file__).resolve().parents[3] / "sample_data" / "dashboard" / "demo_with_review.json"


@st.cache_data(show_spinner=False)
def load_report_cached(path_str: str) -> dict[str, Any]:
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


def main() -> None:
    st.set_page_config(page_title="AMA Migration Dashboard", layout="wide")
    st.title("AMA Migration Dashboard")
    st.caption("Business story + technical truth — same JSON as Excel export.")

    default_path = os.environ.get("AMA_REPORT_PATH", "").strip()
    report_path_resolved: Path | None = None

    with st.sidebar:
        st.header("Report")
        uploaded = st.file_uploader("Or upload JSON", type=["json"], key="json_up")
        path_in = st.text_input("Report path", value=default_path, placeholder="path/to/report.json")

        if uploaded is not None:
            try:
                report = json.loads(uploaded.getvalue().decode("utf-8"))
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
                report = load_report_cached(str(report_path_resolved))
            except OSError as e:
                st.error(f"Cannot read report: {e}")
                return
            except json.JSONDecodeError as e:
                st.error(f"Invalid JSON: {e}")
                return
            if not path_in.strip().lower().endswith(".json"):
                st.warning(
                    "Report path should end in **.json** (e.g. `dash_test.json`). "
                    "A `.js` path will not load the ingestion report."
                )

        st.header("Filters")
        disc = report.get("discovery") or {}
        inv_df = _inventory_df(report)
        if inv_df.empty or "business_domain" not in inv_df.columns:
            domain_opts: list[str] = []
        else:
            domain_opts = sorted({str(x) for x in inv_df["business_domain"].dropna().unique()})
        domains = st.multiselect("Business domain", options=domain_opts, default=[])
        portfolio = st.selectbox("Portfolio section", options=["All", "Core Business", "Technical Debt"])
        conf_min = st.slider("Min confidence", min_value=0.0, max_value=1.0, value=0.0, step=0.05)

    exec_sum = disc.get("executive_summary") or {}
    domain_matrix = exec_sum.get("domain_matrix") or []

    inv_view = inv_df.copy()
    if domains and not inv_view.empty and "business_domain" in inv_view.columns:
        inv_view = inv_view[inv_view["business_domain"].isin(domains)]
    if portfolio and portfolio != "All" and not inv_view.empty and "portfolio_section" in inv_view.columns:
        inv_view = inv_view[inv_view["portfolio_section"] == portfolio]

    risk_set = _high_risk_tables(inv_df, report)
    am = report.get("alias_merge") or {}
    dom_filter = domains if domains else None
    merged_all, review_all, _trash_all = _merge_rows_for_filters(
        report, domains=dom_filter, portfolio=portfolio, conf_min=conf_min
    )

    hitl_file = _hitl_path(report_path_resolved) if report_path_resolved else None
    rp_key = str(report_path_resolved) if report_path_resolved else ""
    if st.session_state.get("hitl_report_key") != rp_key:
        st.session_state.hitl_report_key = rp_key
        if hitl_file:
            st.session_state.hitl = _load_hitl(hitl_file)
        else:
            st.session_state.hitl = {"version": 1, "decisions": {}}

    raw_glossary = build_business_glossary_entries(report)
    glossary = group_glossary_entries(raw_glossary)
    scatter_rows = build_impact_readiness_scatter_rows(report)

    tabs = st.tabs(
        [
            "Executive overview",
            "Domains",
            "Business Glossary",
            "Ask the data",
            "Tables",
            "Review (HITL)",
        ]
    )

    with tabs[0]:
        col1, col2, col3 = st.columns(3)
        pct = _pct_confirmed(am)
        col1.metric("% Confirmed (merge scope)", f"{pct:.1f}%")
        col2.metric("Queries matched", int(report.get("queries_matched") or 0))
        col3.metric("Confirmed columns", len(am.get("merged_entities") or []))

        c1, c2 = st.columns(2)
        with c1:
            if go is not None:
                fig_g = go.Figure(
                    go.Indicator(
                        mode="gauge",
                        value=pct,
                        title={"text": "% Confirmed"},
                        gauge={"axis": {"range": [0, 100]}, "bar": {"color": "darkblue"}},
                    )
                )
                fig_g.update_layout(height=240, margin=dict(l=10, r=10, t=40, b=10))
                st.plotly_chart(fig_g, use_container_width=True, key="exec_gauge_pct_confirmed")
        with c2:
            st.markdown("### Impact vs. readiness")
            st.caption("Big green bubbles = high value + high confidence — migrate first.")
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

        if domain_matrix and px is not None:
            ddf = pd.DataFrame(domain_matrix)
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

    with tabs[1]:
        st.subheader("Domain deep dives")
        st.caption("Data health per domain — how ready the portfolio is to move.")
        dlist = sorted({str(x) for x in inv_df["business_domain"].dropna().unique()}) if not inv_df.empty and "business_domain" in inv_df.columns else []
        first_dom = next((d for d in dlist if d), None)
        for dom in dlist:
            if not dom:
                continue
            dh = domain_data_health(report, dom)
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
                sub = inv_df[inv_df["business_domain"] == dom] if not inv_df.empty else pd.DataFrame()
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
        st.subheader("Business Translator — glossary")
        st.caption(
            "Same mapping on multiple tables is **grouped** into one row so managers see the story once, "
            "with all affected tables listed."
        )
        m1, m2 = st.columns(2)
        m1.metric("Column mappings (raw)", len(raw_glossary))
        m2.metric("Unique business mappings", len(glossary))

        gfilter = st.text_input("Filter glossary", "")
        shown = glossary
        if gfilter.strip():
            gf = gfilter.strip().lower()
            shown = [
                g
                for g in glossary
                if gf in str(g.get("business_term", "")).lower()
                or gf in str(g.get("definition", "")).lower()
                or gf in str(g.get("legacy_columns", "")).lower()
                or gf in " ".join(g.get("source_tables") or []).lower()
            ]
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
            summary_rows.append(
                {
                    "Business term": card.get("business_term"),
                    "Target DDL": card.get("target_ddl"),
                    "Legacy in logs": card.get("legacy_columns"),
                    "Tables (#)": n_tables,
                    "Tables": ", ".join(f"`{t}`" for t in tables if t) or "—",
                    "Confidence": round(float(card.get("confidence_display", card.get("confidence") or 0.0)), 4),
                    "Kind": card.get("kind", ""),
                }
            )
        if summary_rows:
            st.markdown("##### At-a-glance")
            st.dataframe(pd.DataFrame(summary_rows), use_container_width=True, hide_index=True)

        st.markdown("##### Detailed view")
        st.caption(
            "Rows start **collapsed** — click a header to open the full story and confidence gauge. "
            "The summary table above always shows every mapping."
        )
        for i, card in enumerate(shown):
            tables = card.get("source_tables") or ([card.get("source_table")] if card.get("source_table") else [])
            tbl_line = ", ".join(f"`{t}`" for t in tables if t) or "—"
            title = f"{card.get('business_term')} → `{card.get('target_ddl')}` · {len(tables)} table(s)"
            # Include index + sorted tables so Streamlit keys are always unique (avoids duplicate plotly_chart IDs).
            sig = hashlib.sha256(
                f"{i}|{title}|{card.get('legacy_columns')}|{card.get('kind')}|{','.join(sorted(tables))}".encode(
                    "utf-8"
                )
            ).hexdigest()[:32]
            conf = float(card.get("confidence_display", card.get("confidence") or 0.0))
            with st.expander(title, expanded=False):
                cols = st.columns([2, 1])
                with cols[0]:
                    st.markdown(str(card.get("definition", "")))
                    st.markdown(
                        f"**Technical reality:** legacy `{card.get('legacy_columns')}` → "
                        f"target **`{card.get('target_ddl')}`**"
                    )
                    st.markdown(f"**Where it appears:** {tbl_line}")
                    if int(card.get("_group_count") or 1) > 1:
                        st.caption(
                            f"_{int(card.get('_group_count') or 1)} identical merge rows grouped into one view._"
                        )
                    if card.get("reasoning"):
                        st.caption(f"Reasoning: {card.get('reasoning')}")
                with cols[1]:
                    _confidence_gauge(conf, key=f"gloss_plotly_{i}_{sig}")

    with tabs[3]:
        st.subheader("Ask the data")
        st.caption("Heuristic search — Hebrew or English; expands synonyms (e.g. כסף → amount, סכום).")
        aq = st.text_input("Search business concept", placeholder="e.g. revenue, כסף, order status")
        if aq.strip():
            res = semantic_concept_search(report, aq)
            st.markdown("**Tables**")
            st.dataframe(pd.DataFrame(res.get("tables") or []), use_container_width=True, hide_index=True)
            st.markdown("**Column mappings**")
            ch = res.get("column_hits") or []
            if not ch and (res.get("tables") or []):
                st.caption(
                    "No merge/importance rows matched this query for those tables. "
                    "Tables can match via name (e.g. *price* in `pricebooks`) while column hits need "
                    "synonyms (amount, סכום, …) to appear in merged or importance data."
                )
            st.dataframe(pd.DataFrame(ch), use_container_width=True, hide_index=True)
            st.markdown("**Glossary matches**")
            gh = res.get("glossary_hits") or []
            if not gh and (res.get("tables") or []):
                st.caption(
                    "Glossary cards are built from DDL merge results. If nothing matched, try English "
                    "terms (amount, price) or Hebrew סכום — synonyms expand automatically after Unicode normalization."
                )
            for g in gh:
                st.write(f"- **{g.get('business_term')}** → `{g.get('target_ddl')}` ({g.get('source_table')})")

    with tabs[4]:
        st.subheader("Tables")
        q = st.text_input("Search tables / schemas", "", key="tbl_q")
        show = inv_view
        if q.strip() and not show.empty:
            mask = show.astype(str).apply(lambda s: s.str.contains(q, case=False, na=False)).any(axis=1)
            show = show[mask]

        if not show.empty and "full_name" in show.columns:
            show = show.copy()
            show["_risk"] = show["full_name"].map(lambda fn: "High lineage impact" if fn in risk_set else "")
            display_cols = [c for c in show.columns if c != "_risk"]
            st.dataframe(
                show[display_cols + ["_risk"]].rename(columns={"_risk": "Risk"}),
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.dataframe(show, use_container_width=True, hide_index=True)

        tables = sorted(show["full_name"].dropna().unique().tolist()) if not show.empty and "full_name" in show.columns else []
        pick = st.selectbox("Select a table", options=[""] + tables, index=0)

        if pick:
            st.markdown(f"#### `{pick}`")
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

            me = [e for e in (am.get("merged_entities") or []) if str(e.get("source_table")) == pick]
            rev = [e for e in (am.get("review_candidates") or []) if str(e.get("source_table")) == pick]
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

            tr = [e for e in (am.get("trash_candidates") or []) if str(e.get("source_table")) == pick]
            with st.expander("Trash / low-signal", expanded=False):
                if tr:
                    st.dataframe(pd.DataFrame(tr), use_container_width=True, hide_index=True)
                else:
                    st.caption("No trash rows for this table.")

    with tabs[5]:
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
                    st.success("Saved: approved")
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
                    st.warning("Saved: rejected")
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
                    "hide them all. Clear **Business domain** selections, set **Portfolio** to **All**, "
                    "and set **Min confidence** to **0.00**."
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
    st.caption("Filters apply to merge lists, glossary, and HITL queue.")


if __name__ == "__main__":
    main()
