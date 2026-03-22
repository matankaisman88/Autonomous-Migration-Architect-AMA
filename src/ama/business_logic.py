"""
Business domain clustering, executive narrative, and table-level descriptions.

Uses optional OpenAI (AMA_OPENAI_API_KEY / OPENAI_API_KEY) for semantic labels;
falls back to deterministic heuristics so CI and offline runs stay reproducible.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any

TABLE_METADATA_REL = Path("sample_data/ddl/table_metadata.json")
DOMAIN_TAXONOMY_REL = Path("sample_data/ddl/domain_taxonomy.json")


def infer_default_db_from_data_root(data_root: Path, explicit: str | None) -> str:
    """Prefer AMA_DEFAULT_DB; else use the data-root folder name (sanitized)."""
    if explicit and explicit.strip():
        return explicit.strip()
    name = data_root.resolve().name.strip()
    if not name or name in (".", ".."):
        return "LEGACY_CATALOG"
    safe = re.sub(r'[<>:"/\\|?*]', "_", name)
    return safe or "LEGACY_CATALOG"


def load_table_metadata(data_root: Path) -> dict[str, Any]:
    p = data_root / TABLE_METADATA_REL
    if not p.is_file():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _meta_for_table(meta: dict[str, Any], full_name: str, schema: str, table: str) -> dict[str, Any]:
    if not meta:
        return {}
    for key in (full_name, f"{schema}.{table}", table):
        if key and key in meta and isinstance(meta[key], dict):
            return meta[key]
    return {}


def _is_technical_debt(schema: str, table: str, status: str) -> bool:
    s = schema.upper()
    t = table.upper()
    if status == "Ephemeral (Temp)":
        return True
    if "TEMP" in s or "JUNK" in s or "TMP" in t:
        return True
    return False


def _heuristic_domain(schema: str, table: str, full_name: str, comment: str) -> str:
    blob = f"{schema} {table} {comment}".lower()
    if _is_technical_debt(schema, table, ""):
        return "Technical Debt"
    if "legacy_hebrew" in blob or "legacy-hebrew" in blob:
        return "Legacy Core"
    # Hebrew table names (no ASCII letters)
    if table and not re.search(r"[a-zA-Z]", table):
        return "Legacy Core"

    finance_kw = (
        "order",
        "invoice",
        "credit",
        "commission",
        "budget",
        "tax",
        "price",
        "contract",
        "payment",
        "billing",
        "amount",
    )
    logistics_kw = ("ship", "warehouse", "inventory", "product", "stock", "fulfill")
    crm_kw = ("customer", "account", "rep", "territory", "crm", "lead")
    tlow = table.lower()
    if any(k in tlow for k in finance_kw):
        return "Finance"
    if any(k in tlow for k in logistics_kw):
        return "Logistics"
    if any(k in tlow for k in crm_kw):
        return "CRM"
    if "promo" in tlow or "campaign" in tlow:
        return "Marketing"
    if "forecast" in tlow or "target" in tlow or "return" in tlow:
        return "Analytics"
    return "Operations"


def _complexity_for_row(schema: str, table: str, portfolio: str) -> float:
    base = 12.0
    if portfolio == "Technical Debt":
        return min(100.0, base + 40.0)
    if "LEGACY" in schema.upper() or (table and not re.search(r"[a-zA-Z]", table)):
        return min(100.0, base + 28.0)
    if schema.upper() in ("PROD_SALES", "SALES"):
        return min(100.0, base + 6.0)
    return min(100.0, base + 14.0)


def _two_sentence_blurb(
    full_name: str,
    domain: str,
    query_count: int,
    comment: str,
    *,
    llm_text: str | None = None,
) -> str:
    if llm_text and llm_text.strip():
        t = llm_text.strip()
        if len(t) > 400:
            t = t[:397] + "..."
        return t
    c = comment.strip()
    s1 = (
        f"`{full_name}` sits in the {domain} portfolio; log activity (~{query_count} qualifying queries) "
        f"indicates how tightly coupled downstream reporting and operations are to this object."
    )
    if c:
        s2 = f"Documented context: {c} — validate against source-of-truth owners before locking cutover scope."
    else:
        s2 = (
            f"Recommend prioritizing it in the {domain} wave with explicit regression tests on consuming dashboards "
            f"and batch jobs that reference this table."
        )
    return f"{s1} {s2}"


def _openai_enrich(
    items: list[dict[str, Any]],
    *,
    top_for_desc: list[str],
) -> tuple[dict[str, str], dict[str, str]]:
    """
    Returns (domain_by_full_name, description_by_full_name) for LLM-filled strings.
    Empty dicts on failure or missing key.
    """
    api_key = os.environ.get("AMA_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return {}, {}
    model = os.environ.get("AMA_OPENAI_MODEL", "gpt-4o-mini")
    # Compact prompt
    lines = []
    for it in items[:120]:
        lines.append(
            f"- full_name={it['full_name']!r} schema={it['schema']!r} table={it['table']!r} "
            f"semantic_cluster={it.get('dynamic_cluster_id') or ''!r} "
            f"comment={it.get('comment') or ''!r}"
        )
    desc_lines = "\n".join(lines)
    top_s = ", ".join(repr(x) for x in top_for_desc[:12])
    prompt = f"""You are a senior data migration consultant writing for CIO and data steering committees.
Classify SQL tables into business domains for an enterprise cloud migration program.
Domains must be one of: Finance, Logistics, CRM, Marketing, Analytics, Operations, Legacy Core, Technical Debt.
Return a JSON object with keys:
  "domains": [ {{"full_name": string, "domain": string}} , ... ]  (every input table listed exactly once)
  "descriptions": [ {{"full_name": string, "text": string}} , ... ]  (only for these top tables: {top_s})
Each description: exactly two sentences — (1) business impact and dependencies, (2) concrete migration recommendation
(de-risk sequencing, stakeholder alignment, or validation). Avoid generic filler like "shows query volume" or SQL tutorials.
Use decisive consultant language; no exclamation marks.

Tables (semantic_cluster_id may hint related entities):
{desc_lines}
"""
    try:
        import urllib.error
        import urllib.request

        body = json.dumps(
            {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.25,
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions",
            data=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
        text = raw["choices"][0]["message"]["content"]
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*\n", "", text)
            text = re.sub(r"\n```$", "", text)
        data = json.loads(text)
    except Exception:
        return {}, {}

    dom_map: dict[str, str] = {}
    desc_map: dict[str, str] = {}
    for row in data.get("domains") or []:
        if isinstance(row, dict) and row.get("full_name") and row.get("domain"):
            dom_map[str(row["full_name"])] = str(row["domain"])
    for row in data.get("descriptions") or []:
        if isinstance(row, dict) and row.get("full_name") and row.get("text"):
            desc_map[str(row["full_name"])] = str(row["text"])
    return dom_map, desc_map


def _load_domain_taxonomy(data_root: Path) -> dict[str, str]:
    p = data_root / DOMAIN_TAXONOMY_REL
    if not p.is_file():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in raw.items():
        if isinstance(k, str) and isinstance(v, str):
            out[k] = v
    return out


def apply_semantic_domain_clusters(
    prep: list[dict[str, Any]],
    *,
    data_root: Path,
    similarity_threshold: float = 0.88,
) -> None:
    """
    Deterministic hash-embedding clusters over table identity text; optional rollup via domain_taxonomy.json.
    Mutates each prep row with dynamic_cluster_id and optional dynamic_domain_rollup.
    """
    from ama.embeddings import cosine_similarity, hash_embedding

    tax = _load_domain_taxonomy(data_root)
    default_rollup = tax.get("default", "")
    centroids: list[tuple[str, list[float]]] = []
    for row in prep:
        fn = str(row.get("full_name", ""))
        blob = f"{fn} {row.get('schema', '')} {row.get('table', '')} {row.get('comment', '')}".lower()
        vec = hash_embedding(blob, 64)
        label = None
        for lab, cv in centroids:
            if cosine_similarity(vec, cv) >= similarity_threshold:
                label = lab
                break
        if label is None:
            label = f"semantic_cluster_{len(centroids)}"
            centroids.append((label, vec))
        row["dynamic_cluster_id"] = label
        row["dynamic_domain_rollup"] = tax.get(label, default_rollup) or ""


def enrich_executive_risk_hotspots(
    discovery_or_report: dict[str, Any],
    lineage_payload: dict[str, Any] | None = None,
    *,
    max_depth: int = 3,
    top_k: int = 30,
    min_priority: float = 10.0,
) -> None:
    """
    Attach ``discovery.executive_summary.risk_hotspots`` using lineage edges + domain spread (additive).

    Call either as ``enrich_executive_risk_hotspots(discovery, lineage_payload)`` (CLI) or as
    ``enrich_executive_risk_hotspots(report)`` when ``report`` contains top-level ``discovery`` and
    ``lineage`` (dashboard convenience).
    """
    if lineage_payload is None and isinstance(discovery_or_report.get("lineage"), dict):
        discovery = discovery_or_report.get("discovery") or {}
        lineage_payload = discovery_or_report.get("lineage")
    else:
        discovery = discovery_or_report
    if not discovery.get("enabled") or not lineage_payload:
        return
    edges = lineage_payload.get("edges") or []
    if not edges:
        return
    inv = discovery.get("inventory") or []
    table_domain: dict[str, str] = {}
    table_priority: dict[str, float] = {}
    for row in inv:
        if isinstance(row, dict):
            fn = str(row.get("full_name", ""))
            if fn:
                table_domain[fn] = str(row.get("business_domain", "") or "")
                try:
                    table_priority[fn] = float(row.get("priority_score") or 0.0)
                except (TypeError, ValueError):
                    table_priority[fn] = 0.0

    adj: dict[str, set[str]] = defaultdict(set)
    for e in edges:
        if not isinstance(e, dict):
            continue
        a, b = str(e.get("from", "")), str(e.get("to", ""))
        if a and b:
            adj[a].add(b)

    from collections import deque

    hotspots: list[dict[str, Any]] = []
    for row in inv:
        if not isinstance(row, dict):
            continue
        seed = str(row.get("full_name", ""))
        if not seed or float(table_priority.get(seed, 0.0) or 0.0) < min_priority:
            continue
        reached: set[str] = set()
        domains: set[str] = set()
        q: deque[tuple[str, int]] = deque([(seed, 0)])
        while q:
            u, depth = q.popleft()
            if u in reached or depth > max_depth:
                continue
            reached.add(u)
            d = table_domain.get(u, "")
            if d:
                domains.add(d)
            for v in adj.get(u, ()):
                if v not in reached:
                    q.append((v, depth + 1))
        spread = len(domains)
        n_reach = len(reached)
        # Cross-domain blast, broad BFS reach, or any co-query neighbor (lineage aliases may not
        # appear in inventory, so "domains touched" can stay 1 even when the graph has edges).
        has_neighbor = len(adj.get(seed, ())) > 0
        if (
            spread >= 2
            or n_reach >= 4
            or (spread >= 1 and n_reach >= 3)
            or (n_reach >= 2 and has_neighbor)
        ):
            score = min(
                100.0,
                spread * 22.0 + len(reached) * 2.5 + table_priority.get(seed, 0.0) * 0.15,
            )
            hotspots.append(
                {
                    "table": seed,
                    "blast_radius_score": round(score, 1),
                    "domains_touched": sorted(domains),
                    "downstream_tables_reached": len(reached),
                }
            )

    hotspots.sort(key=lambda x: (-float(x.get("blast_radius_score", 0)), str(x.get("table", ""))))
    es = discovery.setdefault("executive_summary", {})
    es["risk_hotspots"] = hotspots[:top_k]


def enrich_discovery_business_context(
    discovery: dict[str, Any],
    *,
    data_root: Path,
    description_top_n: int = 10,
) -> dict[str, Any]:
    """
    Mutates discovery inventory with Business Domain, Portfolio Section, descriptions,
    and attaches executive_summary for Excel. Returns the same discovery dict.
    """
    if not discovery.get("enabled"):
        return discovery
    inv = discovery.get("inventory")
    if not isinstance(inv, list) or not inv:
        discovery["executive_summary"] = {"domain_matrix": [], "table_fact_sheets": []}
        return discovery

    meta_root = load_table_metadata(data_root)
    prep: list[dict[str, Any]] = []
    for row in inv:
        if not isinstance(row, dict):
            continue
        fn = str(row.get("full_name", ""))
        schema = str(row.get("schema", ""))
        table = str(row.get("table", ""))
        st = str(row.get("status", ""))
        m = _meta_for_table(meta_root, fn, schema, table)
        comment = str(m.get("comment") or m.get("description") or "")
        prep.append(
            {
                "full_name": fn,
                "schema": schema,
                "table": table,
                "status": st,
                "query_count": int(row.get("query_count") or 0),
                "comment": comment,
            }
        )

    prep.sort(key=lambda x: (-x["query_count"], x["full_name"]))
    apply_semantic_domain_clusters(prep, data_root=data_root)
    top_names = [p["full_name"] for p in prep[: max(0, description_top_n)]]

    llm_domains, llm_desc = _openai_enrich(prep, top_for_desc=top_names)

    enriched_rows: list[dict[str, Any]] = []
    for row in inv:
        if not isinstance(row, dict):
            enriched_rows.append(row)
            continue
        fn = str(row.get("full_name", ""))
        schema = str(row.get("schema", ""))
        table = str(row.get("table", ""))
        st = str(row.get("status", ""))
        m = _meta_for_table(meta_root, fn, schema, table)
        comment = str(m.get("comment") or m.get("description") or "")

        domain = llm_domains.get(fn) or _heuristic_domain(schema, table, fn, comment)
        if _is_technical_debt(schema, table, st):
            domain = "Technical Debt"
        portfolio = "Technical Debt" if domain == "Technical Debt" else "Core Business"

        qc = int(row.get("query_count") or 0)
        desc = ""
        if fn in top_names:
            desc = _two_sentence_blurb(
                fn,
                domain,
                qc,
                comment,
                llm_text=llm_desc.get(fn),
            )

        out = dict(row)
        out["business_domain"] = domain
        out["portfolio_section"] = portfolio
        out["business_description"] = desc
        out["table_comment"] = comment
        pmatch = next((p for p in prep if p.get("full_name") == fn), None)
        if isinstance(pmatch, dict):
            if pmatch.get("dynamic_cluster_id"):
                out["dynamic_cluster_id"] = pmatch["dynamic_cluster_id"]
            if pmatch.get("dynamic_domain_rollup"):
                out["dynamic_domain_rollup"] = pmatch["dynamic_domain_rollup"]
        enriched_rows.append(out)

    # Sort: Core Business first (by domain, then priority), Technical Debt last
    def _sort_key(r: dict[str, Any]) -> tuple[int, str, float, str]:
        ps = str(r.get("portfolio_section", ""))
        sec = 0 if ps == "Core Business" else 1
        dom = str(r.get("business_domain", ""))
        pr = float(r.get("priority_score") or 0.0)
        return (sec, dom, -pr, str(r.get("full_name", "")))

    enriched_rows.sort(key=_sort_key)
    discovery["inventory"] = enriched_rows

    # Domain matrix: importance vs complexity
    by_dom: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"importance_sum": 0.0, "complexity_sum": 0.0, "n": 0, "schemas": set()}
    )
    for row in enriched_rows:
        dom = str(row.get("business_domain") or "Operations")
        pr = float(row.get("priority_score") or 0.0)
        schema = str(row.get("schema", ""))
        table = str(row.get("table", ""))
        ps = str(row.get("portfolio_section", ""))
        ent = by_dom[dom]
        ent["importance_sum"] += pr
        ent["complexity_sum"] += _complexity_for_row(schema, table, ps)
        ent["n"] += 1
        if schema:
            ent["schemas"].add(schema)

    max_imp = max((v["importance_sum"] for v in by_dom.values()), default=0.0)
    domain_matrix: list[dict[str, Any]] = []
    for dom, ent in sorted(by_dom.items(), key=lambda x: (-x[1]["importance_sum"], x[0])):
        imp = 100.0 * ent["importance_sum"] / max_imp if max_imp > 0 else 0.0
        cx = ent["complexity_sum"] / max(ent["n"], 1)
        cx = min(100.0, max(0.0, cx))
        nschema = len(ent["schemas"])
        narrative = (
            f"{dom} spans {ent['n']} tables across {nschema} schema(s). "
            f"Relative business weight (from log activity) is high when importance is high; "
            f"complexity reflects legacy Hebrew identifiers, cross-schema joins, and ephemeral objects."
        )
        if dom == "Technical Debt":
            narrative = (
                f"Technical Debt groups {ent['n']} low-trust or ephemeral tables. "
                f"Collapse or defer these until core domains are stable."
            )
        domain_matrix.append(
            {
                "business_domain": dom,
                "business_importance": round(imp, 1),
                "migration_complexity": round(cx, 1),
                "table_count": int(ent["n"]),
                "narrative": narrative,
            }
        )

    fact_sheets: list[dict[str, Any]] = []
    for row in enriched_rows:
        fn = str(row.get("full_name", ""))
        if fn in top_names and str(row.get("business_description", "")).strip():
            fact_sheets.append(
                {
                    "full_qualified_name": fn,
                    "business_domain": row.get("business_domain", ""),
                    "query_count": row.get("query_count", 0),
                    "business_description": row.get("business_description", ""),
                }
            )
    fact_sheets.sort(key=lambda r: (-int(r.get("query_count") or 0), str(r.get("full_qualified_name", ""))))

    discovery["executive_summary"] = {
        "domain_matrix": domain_matrix,
        "table_fact_sheets": fact_sheets,
    }
    return discovery


# --- Business glossary & semantic search (dashboard / translator) ---

DDL_BUSINESS_DEFINITIONS: dict[str, str] = {
    "status": "Operational state of a record (open, paid, cancelled) used in workflows and KPIs.",
    "order_id": "Stable identifier linking line items, shipments, and payments to a commercial order.",
    "amount": "Monetary value for revenue, billing, or settlement — the core figure for finance close.",
    "total": "Aggregated monetary or quantity total for reporting and reconciliation.",
    "price": "Unit or list price before discounts — feeds margin and pricing analytics.",
    "quantity": "Counted units sold or moved — drives inventory and fulfillment metrics.",
    "customer_id": "Surrogate key tying activity to a customer account in CRM and finance.",
    "created_at": "When the business event was captured — critical for SLAs and audits.",
    "name": "Human-readable label for the entity (product, customer, or order title).",
    "data": "Generic payload field — often legacy catch‑all; validate before trusting for decisions.",
    "id": "Primary or surrogate identifier — lineage anchor for joins across systems.",
}

# Hebrew / English concept expansion for “Ask the data”
CONCEPT_SYNONYMS: dict[str, list[str]] = {
    "כסף": ["amount", "amt", "סכום", "money", "cash", "revenue", "total", "billing", "price"],
    "money": ["amount", "amt", "סכום", "כסף", "revenue", "total"],
    "revenue": ["amount", "total", "billing", "invoice", "סכום"],
    "churn": ["customer", "status", "cancel", "return"],
    "customer": ["customer", "לקוח", "account", "crm"],
    "order": ["order", "הזמנה", "orders"],
    "status": ["status", "סטטוס", "state"],
}


def humanize_ddl_column(name: str) -> str:
    s = (name or "").strip()
    if not s:
        return "Unknown column"
    return s.replace("_", " ").title()


def business_definition_for_column(canonical: str, domain: str) -> str:
    key = (canonical or "").strip().lower()
    if key in DDL_BUSINESS_DEFINITIONS:
        base = DDL_BUSINESS_DEFINITIONS[key]
    else:
        base = (
            f"Business attribute `{canonical}` in the {domain or 'operational'} domain — "
            f"used in SQL workloads surfaced by AMA discovery."
        )
    if domain:
        return f"{base} (context: **{domain}** portfolio)."
    return base


def _domain_for_table_from_report(report: dict[str, Any], source_table: str) -> str:
    inv = (report.get("discovery") or {}).get("inventory") or []
    for row in inv:
        if isinstance(row, dict) and str(row.get("full_name")) == source_table:
            return str(row.get("business_domain") or "")
    return ""


def build_business_glossary_entries(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Cards for the Business Translator: one row per confirmed/review mapping."""
    am = report.get("alias_merge") or {}
    entries: list[dict[str, Any]] = []
    for i, e in enumerate(am.get("merged_entities") or []):
        if not isinstance(e, dict):
            continue
        st = str(e.get("source_table") or "")
        ddl = str(e.get("canonical_column") or "")
        dom = _domain_for_table_from_report(report, st)
        leg = ", ".join(str(x) for x in (e.get("source_columns") or []))
        try:
            conf = float(e.get("merge_confidence", 0.0))
        except (TypeError, ValueError):
            conf = 0.0
        entries.append(
            {
                "id": f"merged:{i}",
                "business_term": humanize_ddl_column(ddl),
                "definition": business_definition_for_column(ddl, dom),
                "legacy_columns": leg,
                "target_ddl": ddl,
                "confidence": conf,
                "domain": dom,
                "source_table": st,
                "kind": "confirmed",
                "reasoning": " | ".join(e.get("citations") or []) if e.get("citations") else "",
            }
        )
    for i, e in enumerate(am.get("review_candidates") or []):
        if not isinstance(e, dict):
            continue
        st = str(e.get("source_table") or "")
        sug = str(e.get("suggested_ddl") or "")
        leg = str(e.get("legacy_name") or "")
        dom = _domain_for_table_from_report(report, st)
        try:
            conf = float(e.get("merge_confidence", 0.0))
        except (TypeError, ValueError):
            conf = 0.0
        entries.append(
            {
                "id": f"review:{i}",
                "business_term": humanize_ddl_column(sug) if sug else leg or "Review item",
                "definition": business_definition_for_column(sug or leg, dom)
                + " — **needs human confirmation** before cutover.",
                "legacy_columns": leg,
                "target_ddl": sug,
                "confidence": conf,
                "domain": dom,
                "source_table": st,
                "kind": "review",
                "reasoning": str(e.get("citation") or ""),
            }
        )
    return entries


def group_glossary_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Collapse repeated cards when the same business mapping appears on multiple tables
    (e.g. `status` ← `status` on both `orders` and `orders_as_o`).
    """
    buckets: dict[str, dict[str, Any]] = {}
    order_keys: list[str] = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        key = "|".join(
            [
                str(e.get("kind", "")),
                str(e.get("target_ddl", "")).lower().strip(),
                str(e.get("legacy_columns", "")).strip(),
                str(e.get("business_term", "")).strip(),
            ]
        )
        st = str(e.get("source_table") or "").strip()
        try:
            conf = float(e.get("confidence") or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        if key not in buckets:
            order_keys.append(key)
            row = dict(e)
            row["source_tables"] = [st] if st else []
            row["confidence_display"] = conf
            row["_group_count"] = 1
            buckets[key] = row
        else:
            b = buckets[key]
            if st and st not in b["source_tables"]:
                b["source_tables"].append(st)
            b["confidence_display"] = max(float(b.get("confidence_display") or 0.0), conf)
            b["_group_count"] = int(b.get("_group_count") or 1) + 1
    return [buckets[k] for k in order_keys]


def domain_data_health(report: dict[str, Any], domain: str) -> dict[str, Any]:
    """Metrics for Domain deep dive: confirmation rate, avg importance, risk label."""
    inv = (report.get("discovery") or {}).get("inventory") or []
    tables = {
        str(r.get("full_name"))
        for r in inv
        if isinstance(r, dict) and str(r.get("business_domain") or "") == domain and r.get("full_name")
    }
    am = report.get("alias_merge") or {}
    me = [e for e in (am.get("merged_entities") or []) if isinstance(e, dict) and str(e.get("source_table")) in tables]
    rev = [e for e in (am.get("review_candidates") or []) if isinstance(e, dict) and str(e.get("source_table")) in tables]
    tr = [e for e in (am.get("trash_candidates") or []) if isinstance(e, dict) and str(e.get("source_table")) in tables]
    tot = len(me) + len(rev) + len(tr)
    pct = 100.0 * len(me) / tot if tot else 0.0
    imp_rows = [
        r
        for r in (report.get("importance_ddl") or [])
        if isinstance(r, dict) and str(r.get("source_table") or "") in tables
    ]
    avg_imp = 0.0
    if imp_rows:
        vals = []
        for r in imp_rows:
            try:
                vals.append(float(r.get("importance_score", 0.0)))
            except (TypeError, ValueError):
                pass
        avg_imp = sum(vals) / len(vals) if vals else 0.0
    if pct >= 70:
        risk = "Low"
    elif pct >= 40:
        risk = "Medium"
    else:
        risk = "High"
    if domain == "Technical Debt":
        risk = "High"
    return {
        "domain": domain,
        "table_count": len(tables),
        "pct_columns_confirmed": round(pct, 1),
        "avg_importance": round(avg_imp, 4),
        "risk_level": risk,
        "n_confirmed": len(me),
        "n_review": len(rev),
        "n_trash": len(tr),
    }


def _nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s or "")


def expand_concept_query(query: str) -> list[str]:
    """Return search needles including Hebrew/English synonym expansion (Unicode-safe)."""
    q = (query or "").strip()
    if not q:
        return []
    qn = _nfc(q)
    out: list[str] = [q, qn]
    low = qn.lower()
    for key, syns in CONCEPT_SYNONYMS.items():
        kn = _nfc(key)
        if kn in qn or qn in kn or kn.lower() in low or key.lower() in low:
            out.extend(syns)
            out.append(kn)
        for s in syns:
            sn = _nfc(str(s))
            if not sn:
                continue
            if sn.lower() in low or sn in qn:
                out.extend([kn] + list(syns))
                break
    # de-dupe preserving order
    seen: set[str] = set()
    uniq: list[str] = []
    for x in out:
        t = x.strip()
        if not t or t in seen:
            continue
        seen.add(t)
        uniq.append(t)
    return uniq


def semantic_concept_search(report: dict[str, Any], query: str) -> dict[str, Any]:
    """
    Heuristic search across inventory, merge results, and glossary entries.
    Matches if any expanded needle appears in the concatenated text (case-insensitive for ASCII).
    """
    needles = expand_concept_query(query)
    if not needles:
        return {"tables": [], "column_hits": [], "glossary_hits": []}

    def _match_blob(blob: str) -> bool:
        if not blob:
            return False
        blob_n = _nfc(blob)
        blob_low = blob_n.lower()
        for n in needles:
            nn = _nfc(n)
            if not nn:
                continue
            if nn in blob_n:
                return True
            if nn.isascii() and nn.lower() in blob_low:
                return True
        return False

    inv = (report.get("discovery") or {}).get("inventory") or []
    tables_out: list[dict[str, Any]] = []
    for r in inv:
        if not isinstance(r, dict):
            continue
        blob = " ".join(
            str(r.get(k) or "")
            for k in ("full_name", "business_domain", "business_description", "table_comment", "schema", "table")
        )
        if _match_blob(blob):
            tables_out.append(
                {
                    "full_name": r.get("full_name"),
                    "domain": r.get("business_domain"),
                    "queries": r.get("query_count"),
                    "snippet": (r.get("business_description") or "")[:240],
                }
            )

    col_hits: list[dict[str, Any]] = []
    am = report.get("alias_merge") or {}
    for e in am.get("merged_entities") or []:
        if not isinstance(e, dict):
            continue
        blob = " ".join(
            [str(e.get("canonical_column")), str(e.get("source_table"))]
            + [str(x) for x in (e.get("source_columns") or [])]
        )
        if _match_blob(blob):
            col_hits.append(
                {
                    "role": "confirmed",
                    "source_table": e.get("source_table"),
                    "ddl": e.get("canonical_column"),
                    "legacy": ", ".join(str(x) for x in (e.get("source_columns") or [])),
                }
            )
    for r in report.get("importance_ddl") or []:
        if not isinstance(r, dict):
            continue
        col = str(r.get("column", "") or "")
        st = str(r.get("source_table", "") or "")
        logged = r.get("logged_as")
        extra = ""
        if isinstance(logged, list):
            extra = " ".join(str(x) for x in logged)
        blob = f"{col} {st} {extra}"
        if _match_blob(blob):
            col_hits.append(
                {
                    "role": "importance_tracked",
                    "source_table": st,
                    "ddl": col,
                    "legacy": extra or "(see merge / logs)",
                }
            )
    for e in am.get("review_candidates") or []:
        if not isinstance(e, dict):
            continue
        blob = " ".join([str(e.get("legacy_name")), str(e.get("suggested_ddl")), str(e.get("source_table"))])
        if _match_blob(blob):
            col_hits.append(
                {
                    "role": "review",
                    "source_table": e.get("source_table"),
                    "ddl": e.get("suggested_ddl"),
                    "legacy": str(e.get("legacy_name")),
                }
            )

    gloss = build_business_glossary_entries(report)
    ghits: list[dict[str, Any]] = []
    for g in gloss:
        parts = [
            str(g.get("business_term") or ""),
            str(g.get("definition") or ""),
            str(g.get("legacy_columns") or ""),
            str(g.get("target_ddl") or ""),
            str(g.get("reasoning") or ""),
            str(g.get("domain") or ""),
            str(g.get("source_table") or ""),
        ]
        if _match_blob(" ".join(parts)):
            ghits.append(g)

    seen_ch: set[tuple[str, str, str, str]] = set()
    deduped: list[dict[str, Any]] = []
    for h in col_hits:
        k = (
            str(h.get("role") or ""),
            str(h.get("source_table") or ""),
            str(h.get("ddl") or ""),
            str(h.get("legacy") or ""),
        )
        if k in seen_ch:
            continue
        seen_ch.add(k)
        deduped.append(h)

    return {"tables": tables_out, "column_hits": deduped, "glossary_hits": ghits}


def build_impact_readiness_scatter_rows(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Points for Plotly: importance (Y), confidence (X), bubble size ~ query volume."""
    imp = {}
    for r in report.get("importance_ddl") or []:
        if not isinstance(r, dict):
            continue
        col = str(r.get("column", ""))
        st = str(r.get("source_table") or "")
        try:
            v = float(r.get("importance_score", 0.0))
        except (TypeError, ValueError):
            v = 0.0
        if st and col:
            imp[f"{st}::{col}"] = v

    qvol: dict[str, float] = {}
    for row in (report.get("discovery") or {}).get("inventory") or []:
        if not isinstance(row, dict):
            continue
        fn = str(row.get("full_name") or "")
        try:
            qvol[fn] = float(row.get("query_count") or 0.0)
        except (TypeError, ValueError):
            qvol[fn] = 0.0

    rows: list[dict[str, Any]] = []
    am = report.get("alias_merge") or {}
    for e in am.get("merged_entities") or []:
        if not isinstance(e, dict):
            continue
        st = str(e.get("source_table") or "")
        ddl = str(e.get("canonical_column") or "")
        try:
            conf = float(e.get("merge_confidence", 0.0))
        except (TypeError, ValueError):
            conf = 0.0
        importance = float(imp.get(f"{st}::{ddl}", imp.get(ddl, 0.0)))
        rows.append(
            {
                "label": f"{st} :: {ddl}",
                "source_table": st,
                "ddl": ddl,
                "confidence": conf,
                "importance": importance,
                "query_volume": qvol.get(st, 0.0),
            }
        )
    return rows


def review_row_signature(row: dict[str, Any]) -> str:
    import hashlib

    raw = "|".join(
        [
            str(row.get("source_table") or ""),
            str(row.get("legacy_name") or ""),
            str(row.get("suggested_ddl") or ""),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
