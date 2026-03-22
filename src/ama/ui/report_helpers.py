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
