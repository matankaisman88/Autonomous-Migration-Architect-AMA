"""
Pure helpers for parsing AMA JSON reports (Streamlit-agnostic).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd


def load_report_json(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    return json.loads(p.read_text(encoding="utf-8"))


def _inventory_df(report: dict[str, Any]) -> pd.DataFrame:
    disc = report.get("discovery") or {}
    inv = disc.get("inventory") or []
    if not inv:
        return pd.DataFrame()
    return pd.DataFrame(inv)


def _domain_for_table(report: dict[str, Any], full_name: str) -> str:
    df = _inventory_df(report)
    if df.empty or "full_name" not in df.columns:
        return ""
    m = df[df["full_name"] == full_name]
    if m.empty:
        return ""
    return str(m.iloc[0].get("business_domain") or "")


def _pct_confirmed(am: dict[str, Any]) -> float:
    me = am.get("merged_entities") or []
    rev = am.get("review_candidates") or []
    tr = am.get("trash_candidates") or []
    tot = len(me) + len(rev) + len(tr)
    if tot == 0:
        return 0.0
    return 100.0 * len(me) / tot


def pct_confirmed_filtered(
    merged_all: list[dict[str, Any]],
    review_all: list[dict[str, Any]],
    trash_all: list[dict[str, Any]],
) -> float:
    """% confirmed using sidebar-filtered merge buckets (same logic as :func:`_pct_confirmed`)."""
    tot = len(merged_all) + len(review_all) + len(trash_all)
    if tot == 0:
        return 0.0
    return 100.0 * len(merged_all) / tot


def filter_domain_matrix_rows(
    domain_matrix: list[dict[str, Any]],
    *,
    domains: list[str] | None,
) -> list[dict[str, Any]]:
    """Keep matrix rows whose ``business_domain`` is in the sidebar multiselect (if any)."""
    if not domain_matrix or not domains:
        return domain_matrix
    doms = {str(d) for d in domains}
    return [r for r in domain_matrix if isinstance(r, dict) and str(r.get("business_domain") or "") in doms]


def filter_risk_hotspots(
    hotspots: list[dict[str, Any]],
    *,
    allowed_tables: set[str] | None,
) -> list[dict[str, Any]]:
    """Keep hotspot rows whose ``table`` is in the filtered inventory (if ``allowed_tables`` is set)."""
    if not hotspots:
        return hotspots
    if allowed_tables is None:
        return hotspots
    if len(allowed_tables) == 0:
        return []
    return [h for h in hotspots if isinstance(h, dict) and str(h.get("table") or "") in allowed_tables]


def filter_semantic_search_results(
    res: dict[str, Any],
    *,
    allowed_tables: set[str] | None,
) -> dict[str, Any]:
    """Restrict Ask-the-data hits to tables still in scope after sidebar filters."""
    if allowed_tables is None:
        return res
    if len(allowed_tables) == 0:
        return {"tables": [], "column_hits": [], "glossary_hits": []}
    out = dict(res)
    tabs = []
    for r in res.get("tables") or []:
        if isinstance(r, dict) and str(r.get("full_name") or "") in allowed_tables:
            tabs.append(r)
    out["tables"] = tabs
    ch = []
    for r in res.get("column_hits") or []:
        if isinstance(r, dict) and str(r.get("source_table") or "") in allowed_tables:
            ch.append(r)
    out["column_hits"] = ch
    gh = []
    for r in res.get("glossary_hits") or []:
        if isinstance(r, dict) and str(r.get("source_table") or "") in allowed_tables:
            gh.append(r)
    out["glossary_hits"] = gh
    return out


def inventory_allowed_tables(inv_view: pd.DataFrame) -> set[str] | None:
    """Return full_name set for filter helpers; empty frame → ``None`` (no table restriction)."""
    if inv_view.empty or "full_name" not in inv_view.columns:
        return None
    return {str(x) for x in inv_view["full_name"].dropna().unique()}


def _merge_rows_for_filters(
    report: dict[str, Any],
    *,
    domains: list[str] | None,
    portfolio: str | None,
    conf_min: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    am = report.get("alias_merge") or {}
    inv = _inventory_df(report)
    table_to_domain: dict[str, str] = {}
    table_to_portfolio: dict[str, str] = {}
    if not inv.empty:
        for _, r in inv.iterrows():
            fn = str(r.get("full_name") or "")
            if fn:
                table_to_domain[fn] = str(r.get("business_domain") or "")
                table_to_portfolio[fn] = str(r.get("portfolio_section") or "")

    def _keep(ent: dict[str, Any]) -> bool:
        stbl = str(ent.get("source_table") or "")
        try:
            c = float(ent.get("merge_confidence", 0.0))
        except (TypeError, ValueError):
            c = 0.0
        if c < conf_min:
            return False
        dom = table_to_domain.get(stbl, "")
        if domains is not None and len(domains) > 0:
            if not dom or dom not in domains:
                return False
        ps = table_to_portfolio.get(stbl, "")
        if portfolio and portfolio != "All":
            if ps and ps != portfolio:
                return False
        return True

    merged = [e for e in (am.get("merged_entities") or []) if isinstance(e, dict) and _keep(e)]
    review = [e for e in (am.get("review_candidates") or []) if isinstance(e, dict) and _keep(e)]
    trash = [e for e in (am.get("trash_candidates") or []) if isinstance(e, dict) and _keep(e)]
    return merged, review, trash


def filter_glossary_grouped(
    grouped: list[dict[str, Any]],
    *,
    report: dict[str, Any],
    domains: list[str] | None,
    portfolio: str | None,
    conf_min: float,
) -> list[dict[str, Any]]:
    """
    Apply the same rules as :func:`_merge_rows_for_filters` to grouped Business Glossary cards.

    A card is kept if ``confidence_display`` (or ``confidence``) is >= ``conf_min`` and at least one
    ``source_table`` passes domain + portfolio (when those filters are set).
    """
    inv = _inventory_df(report)
    table_to_domain: dict[str, str] = {}
    table_to_portfolio: dict[str, str] = {}
    if not inv.empty:
        for _, r in inv.iterrows():
            fn = str(r.get("full_name") or "")
            if fn:
                table_to_domain[fn] = str(r.get("business_domain") or "")
                table_to_portfolio[fn] = str(r.get("portfolio_section") or "")

    out: list[dict[str, Any]] = []
    for e in grouped:
        if not isinstance(e, dict):
            continue
        try:
            c = float(e.get("confidence_display", e.get("confidence") or 0.0))
        except (TypeError, ValueError):
            c = 0.0
        if str(e.get("kind", "")).lower() == "glossary_source":
            if c >= conf_min:
                out.append(e)
            continue
        if c < conf_min:
            continue
        tables = e.get("source_tables") or ([e.get("source_table")] if e.get("source_table") else [])
        tables = [str(t).strip() for t in tables if t]
        if not tables:
            dom = str(e.get("domain") or "")
            if domains is not None and len(domains) > 0:
                if not dom or dom not in domains:
                    continue
            out.append(e)
            continue
        any_ok = False
        for stbl in tables:
            dom = table_to_domain.get(stbl, "")
            if domains is not None and len(domains) > 0:
                if not dom or dom not in domains:
                    continue
            ps = table_to_portfolio.get(stbl, "")
            if portfolio and portfolio != "All":
                if ps and ps != portfolio:
                    continue
            any_ok = True
            break
        if any_ok:
            out.append(e)
    return out


def _high_risk_tables(inv: pd.DataFrame, report: dict[str, Any], threshold: float = 70.0) -> set[str]:
    out: set[str] = set()
    disc = report.get("discovery") or {}
    es = disc.get("executive_summary") or {}
    for row in es.get("risk_hotspots") or []:
        if isinstance(row, dict):
            t = str(row.get("table") or "")
            if t:
                out.add(t)
    if not inv.empty and "priority_score" in inv.columns:
        for _, r in inv.iterrows():
            try:
                ps = float(r.get("priority_score") or 0.0)
            except (TypeError, ValueError):
                ps = 0.0
            if ps >= threshold:
                fn = str(r.get("full_name") or "")
                if fn:
                    out.add(fn)
    for row in report.get("importance_ddl") or []:
        if not isinstance(row, dict):
            continue
        try:
            score = float(row.get("importance_score") or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        if score >= 0.35:
            st = str(row.get("source_table") or "")
            if st:
                out.add(st)
    return out


def _importance_lookup(report: dict[str, Any]) -> dict[str, float]:
    out: dict[str, float] = {}
    for row in report.get("importance_ddl") or []:
        if not isinstance(row, dict):
            continue
        col = str(row.get("column", ""))
        st = str(row.get("source_table", "") or "")
        try:
            v = float(row.get("importance_score", 0.0))
        except (TypeError, ValueError):
            v = 0.0
        if st and col:
            out[f"{st}::{col}"] = v
        if col:
            out[col] = max(out.get(col, 0.0), v)
    return out
