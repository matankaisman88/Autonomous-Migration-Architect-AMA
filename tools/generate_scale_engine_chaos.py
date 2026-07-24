from __future__ import annotations

import argparse
import hashlib
import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ama.business_logic import build_glossary_source_report
from ama.schemas.report import AmaReportBoundarySchema


DOMAINS = ["Finance", "Operations", "Logistics", "CRM", "Technical Debt"]


@dataclass
class TableSpec:
    full_name: str
    business_domain: str
    columns: list[dict[str, str]]
    query_count: int
    outgoing_edges: int = 0
    sample_rows: list[dict[str, Any]] | None = None
    notes: str = ""


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def _stable_index(seed_text: str, modulo: int) -> int:
    digest = hashlib.sha256(seed_text.encode("utf-8")).hexdigest()
    return int(digest[:12], 16) % max(1, modulo)


def _build_glossary() -> dict[str, str]:
    # 30+ mappings, includes exact, fuzzy-supporting tokens, and orphan entries.
    return {
        "מזהה_הזמנה": "order_id",
        "מזהה_לקוח": "customer_id",
        "שם_לקוח": "customer_name",
        "סטטוס_משלוח": "shipment_status",
        "קוד_מוביל": "carrier_code",
        "קוד_מסלול": "route_code",
        "תאריך_יצירה": "created_at",
        "תאריך_עדכון": "updated_at",
        "מזהה_משלוח": "shipment_id",
        "כתובת_יעד": "destination_address",
        "תאריך_מסירה": "delivered_at",
        "תאריך_קבלה": "received_at",
        "מזהה_חשבונית": "invoice_id",
        "מזהה_מסמך": "doc_id",
        "ערך_מעמ": "tax_value",
        "סכום_כולל": "total_amount",
        "יתרה": "balance",
        "זיכוי_מס": "tax_credit",
        "סכום": "amount",
        "מזהה_פרופיל": "profile_id",
        "שם_פרטי": "first_name",
        "שם_משפחה": "last_name",
        "דואל": "email_address",
        "סטטוס": "status",
        "מזהה_מקור": "source_id",
        "מטען_גולמי": "raw_payload",
        "הערות": "notes",
        "account_identifier": "account_id",
        "order_identifier": "order_id",
        "customer_identifier": "customer_id",
        "legacy_reference": "legacy_ref",
        "שדה_מסירה_1": "ds_col_1",
        "שדה_מסירה_2": "ds_col_2",
        "שדה_מסירה_3": "ds_col_3",
        "שדה_מסירה_4": "ds_col_4",
        "שדה_מסירה_5": "ds_col_5",
        "שדה_מסירה_6": "ds_col_6",
        "שדה_מסירה_7": "ds_col_7",
        "שדה_מסירה_8": "ds_col_8",
        "שדה_מסירה_9": "ds_col_9",
        "שדה_מסירה_10": "ds_col_10",
        "שדה_ביניים_1": "y_col_1",
        "שדה_ביניים_2": "y_col_2",
        "שדה_ביניים_3": "y_col_3",
        "שדה_ביניים_4": "y_col_4",
        "שדה_ביניים_5": "y_col_5",
        "שדה_ביניים_6": "y_col_6",
        "שדה_ביניים_7": "y_col_7",
        "שדה_ביניים_8": "y_col_8",
        "שדה_ביניים_9": "y_col_9",
        "שדה_ביניים_10": "y_col_10",
        "unused_term_alpha": "orphan_alpha",
        "unused_term_beta": "orphan_beta",
        "unused_term_gamma": "orphan_gamma",
        "unused_term_delta": "orphan_delta",
        "unused_term_epsilon": "orphan_epsilon",
    }


def _exact_green_columns() -> list[dict[str, str]]:
    return [
        {"name": "ds_col_1", "type": "nvarchar"},
        {"name": "ds_col_2", "type": "nvarchar"},
        {"name": "ds_col_3", "type": "nvarchar"},
        {"name": "ds_col_4", "type": "nvarchar"},
        {"name": "ds_col_5", "type": "nvarchar"},
        {"name": "ds_col_6", "type": "datetime2"},
        {"name": "ds_col_7", "type": "datetime2"},
        {"name": "ds_col_8", "type": "nvarchar"},
        {"name": "ds_col_9", "type": "datetime2"},
        {"name": "ds_col_10", "type": "datetime2"},
    ]


def _build_named_tables() -> list[TableSpec]:
    return [
        TableSpec(
            full_name="finance.invoice_attachments",
            business_domain="Finance",
            columns=[
                {"name": "invoice_id", "type": "int"},
                {"name": "attachment", "type": "varbinary"},
                {"name": "uploaded_at", "type": "datetime2"},
            ],
            query_count=20,
            notes="Pure BLOB trigger",
        ),
        TableSpec(
            full_name="legacy.document_archive",
            business_domain="Technical Debt",
            columns=[
                {"name": "doc_id", "type": "int"},
                {"name": "content", "type": "ntext"},
                {"name": "created_at", "type": "datetime"},
            ],
            query_count=12,
            notes="NTEXT BLOCK trigger",
        ),
        TableSpec(
            full_name="sales.orders",
            business_domain="Operations",
            columns=[
                {"name": "order_id", "type": "int"},
                {"name": "customer_id", "type": "int"},
                {"name": "status", "type": "nvarchar"},
                {"name": "created_at", "type": "datetime2"},
            ],
            query_count=180,
            outgoing_edges=1,
            notes="Type inconsistency pair",
        ),
        TableSpec(
            full_name="crm.orders",
            business_domain="Operations",
            columns=[
                {"name": "order_id", "type": "int"},
                {"name": "customer_id", "type": "varchar(50)"},
                {"name": "status", "type": "nvarchar"},
                {"name": "created_at", "type": "datetime2"},
            ],
            query_count=160,
            outgoing_edges=1,
            notes="Type inconsistency pair",
        ),
        TableSpec(
            full_name="logistics.shipments",
            business_domain="Logistics",
            columns=[
                {"name": "shipment_id", "type": "int"},
                {"name": "carrier_code", "type": "char(3)"},
                {"name": "created_at", "type": "datetime2"},
            ],
            query_count=140,
            outgoing_edges=1,
            notes="Type inconsistency pair",
        ),
        TableSpec(
            full_name="logistics.returns",
            business_domain="Logistics",
            columns=[
                {"name": "return_id", "type": "int"},
                {"name": "carrier_code", "type": "nvarchar(10)"},
                {"name": "created_at", "type": "datetime2"},
            ],
            query_count=110,
            outgoing_edges=1,
            notes="Type inconsistency pair",
        ),
        TableSpec(
            full_name="finance.mega_journal",
            business_domain="Finance",
            columns=[{"name": f"journal_col_{i}", "type": "nvarchar"} for i in range(60)],
            query_count=90,
            notes="Column count outlier",
        ),
        TableSpec(
            full_name="operations.wide_staging",
            business_domain="Operations",
            columns=[{"name": f"stg_col_{i}", "type": "nvarchar"} for i in range(45)],
            query_count=120,
            notes="Column count outlier",
        ),
        TableSpec(
            full_name="finance.core_ledger",
            business_domain="Finance",
            columns=[
                {"name": "ledger_id", "type": "int"},
                {"name": "balance", "type": "money"},
                {"name": "total_amount", "type": "money"},
                {"name": "tax_credit", "type": "money"},
                {"name": "created_at", "type": "datetime2"},
                {"name": "updated_at", "type": "datetime2"},
            ],
            query_count=800,
            outgoing_edges=5,
            notes="Criticality max edge",
        ),
        TableSpec(
            full_name="logistics.delivery_status",
            business_domain="Logistics",
            columns=_exact_green_columns(),
            query_count=5,
            outgoing_edges=0,
            notes="Confidence=100 green edge",
        ),
        TableSpec(
            full_name="technical_debt.tbl_junk_7",
            business_domain="Technical Debt",
            columns=[
                {"name": "qzz_a1", "type": "xml"},
                {"name": "qzz_a2", "type": "cursor"},
                {"name": "qzz_a3", "type": "xml"},
                {"name": "qzz_b1", "type": "cursor"},
                {"name": "qzz_b2", "type": "xml"},
                {"name": "qzz_b3", "type": "cursor"},
            ],
            query_count=8,
            notes="Confidence=0 edge",
        ),
        TableSpec(
            full_name="finance.payment_staging",
            business_domain="Finance",
            columns=[
                {"name": "payment_id", "type": "int"},
                {"name": "customer_id", "type": "int"},
                {"name": "staging_status", "type": "nvarchar"},
                {"name": "created_at", "type": "datetime2"},
                {"name": "updated_at", "type": "datetime2"},
                {"name": "legacy_ref", "type": "nvarchar"},
                {"name": "raw_payload", "type": "nvarchar"},
                {"name": "processing_step", "type": "nvarchar"},
                {"name": "attempt_no", "type": "int"},
                {"name": "approved_by", "type": "nvarchar"},
            ],
            query_count=320,
            outgoing_edges=2,
            notes="Mixed signal edge",
        ),
        TableSpec(
            full_name="crm.CustomerProfiles",
            business_domain="CRM",
            columns=[
                {"name": "firstName", "type": "nvarchar"},
                {"name": "lastName", "type": "nvarchar"},
                {"name": "emailAddress", "type": "nvarchar"},
                {"name": "created_at", "type": "datetime2"},
                {"name": "updated_at", "type": "datetime2"},
                {"name": "source_id", "type": "int"},
            ],
            query_count=60,
            notes="Naming entropy INFO edge",
        ),
        TableSpec(
            full_name="operations.import_staging",
            business_domain="Operations",
            columns=[
                {"name": "import_id", "type": "int"},
                {"name": "raw_payload", "type": "nvarchar"},
                {"name": "created_at", "type": "datetime2"},
                {"name": "source_id", "type": "int"},
            ],
            query_count=70,
            sample_rows=[
                {"import_id": 1, "raw_payload": None, "created_at": None, "source_id": None},
                {"import_id": 2, "raw_payload": None, "created_at": None, "source_id": None},
                {"import_id": 3, "raw_payload": None, "created_at": "2025-01-01", "source_id": None},
                {"import_id": 4, "raw_payload": None, "created_at": None, "source_id": None},
            ],
            notes="NULL-rate WARN edge",
        ),
        TableSpec(
            full_name="unclassified.mystery_a",
            business_domain="Unclassified",
            columns=[
                {"name": "mystery_id", "type": "int"},
                {"name": "payload", "type": "nvarchar"},
            ],
            query_count=4,
            notes="Cluster boundary edge",
        ),
        TableSpec(
            full_name="unclassified.mystery_b",
            business_domain="Unclassified",
            columns=[
                {"name": "mystery_id", "type": "int"},
                {"name": "payload", "type": "nvarchar"},
                {"name": "created_at", "type": "datetime2"},
            ],
            query_count=3,
            notes="Cluster boundary edge",
        ),
        TableSpec(
            full_name="unclassified.mystery_c",
            business_domain="Unclassified",
            columns=[
                {"name": "mystery_id", "type": "int"},
                {"name": "payload", "type": "nvarchar"},
                {"name": "status", "type": "nvarchar"},
            ],
            query_count=2,
            notes="Cluster boundary edge",
        ),
    ]


def _mk_filler_table(idx: int, rng: random.Random, queue_hint: str) -> TableSpec:
    domain = DOMAINS[idx % len(DOMAINS)]
    schema = domain.lower().replace(" ", "_")
    name = f"{schema}.chaos_{queue_hint}_{idx:03d}"
    outgoing = 0
    if queue_hint == "red":
        cols = [
            {"name": f"rx_{idx}_a", "type": "xml"},
            {"name": f"rx_{idx}_b", "type": "cursor"},
            {"name": f"rx_{idx}_c", "type": "xml"},
            {"name": f"rx_{idx}_d", "type": "textlike"},
            {"name": f"rx_{idx}_e", "type": "bytea"},
            {"name": f"rx_{idx}_f", "type": "variant"},
            {"name": f"rx_{idx}_g", "type": "varchar"},
            {"name": f"rx_{idx}_h", "type": "uuid"},
        ]
        qcount = 6 + (idx % 8)
    elif queue_hint == "yellow":
        cols = [
            {"name": "y_col_1", "type": "nvarchar"},
            {"name": "y_col_2", "type": "nvarchar"},
            {"name": "y_col_3", "type": "json"},
            {"name": "y_col_4", "type": "json"},
            {"name": "y_col_5", "type": "json"},
            {"name": "y_col_6", "type": "json"},
            {"name": "y_col_7", "type": "json"},
            {"name": "y_col_8", "type": "json"},
            {"name": "y_col_9", "type": "json"},
            {"name": "y_col_10", "type": "json"},
        ]
        qcount = 250 + (idx % 100)
        outgoing = 1
    else:
        cols = _exact_green_columns()
        qcount = 3 + (idx % 8)

    if idx % 19 == 0:
        qcount = 620 + (idx % 30)  # ensure >500 population
    if idx % 7 == 0 and qcount < 50:
        qcount = 70 + (idx % 100)  # ensure 50-200 band coverage
    if idx % 11 == 0:
        outgoing = 3 + (idx % 3)  # additional high-lineage tables

    return TableSpec(
        full_name=name,
        business_domain=domain,
        columns=cols,
        query_count=qcount,
        outgoing_edges=outgoing,
        notes=f"Generated {queue_hint} filler with seed={rng.randint(0, 9999)}",
    )


def _build_table_specs(total_tables: int, seed: int) -> list[TableSpec]:
    named = _build_named_tables()
    if total_tables < 80:
        raise ValueError("--tables must be >= 80")
    if total_tables < len(named):
        raise ValueError(f"--tables must be >= {len(named)} to include mandatory edge-case tables")

    remaining = total_tables - len(named)
    green_target = int(round(total_tables * 0.30))
    yellow_target = int(round(total_tables * 0.40))
    red_target = total_tables - green_target - yellow_target

    # Roughly account for named tables we intentionally place by queue.
    named_green = 1  # logistics.delivery_status
    named_yellow = 7  # outlier/null/name-entropy/small-cluster rows
    named_red = 10  # explicit RED edges
    extra_green = max(0, green_target - named_green)
    extra_yellow = max(0, yellow_target - named_yellow)
    extra_red = max(0, red_target - named_red)

    queue_plan: list[str] = (["green"] * extra_green) + (["yellow"] * extra_yellow) + (["red"] * extra_red)
    while len(queue_plan) < remaining:
        queue_plan.append(["green", "yellow", "red"][len(queue_plan) % 3])
    queue_plan = queue_plan[:remaining]

    rng = random.Random(seed)
    rng.shuffle(queue_plan)
    fillers = [_mk_filler_table(i, rng, qh) for i, qh in enumerate(queue_plan)]
    return named + fillers


def _build_lineage(specs: list[TableSpec]) -> list[dict[str, str]]:
    edges: list[dict[str, str]] = []
    existing = {s.full_name for s in specs}
    for s in specs:
        for i in range(s.outgoing_edges):
            tgt = f"{s.full_name}__downstream_{i+1}"
            if tgt not in existing:
                # Link to an existing table in deterministic way.
                tgt = specs[_stable_index(f"{s.full_name}|{i}", len(specs))].full_name
                if tgt == s.full_name:
                    tgt = specs[_stable_index(f"{s.full_name}|{i}|alt", len(specs))].full_name
            if tgt != s.full_name:
                edges.append({"source": s.full_name, "target": tgt})
    # Guarantee additional 3+ lineage tables.
    for src in ("finance.chaos_red_003", "operations.chaos_yellow_017", "crm.chaos_green_029"):
        if src in existing:
            for j in range(3):
                tgt = specs[_stable_index(f"{src}|{j}|forced", len(specs))].full_name
                if tgt != src:
                    edges.append({"source": src, "target": tgt})
    return edges


def _build_alias_merge(specs: list[TableSpec], glossary: dict[str, str], manifest_path: Path) -> dict[str, Any]:
    merged_entities: list[dict[str, Any]] = []
    review_candidates: list[dict[str, Any]] = []
    trash_candidates: list[dict[str, Any]] = []
    merge_seed_specs = [s for s in specs if s.full_name != "technical_debt.tbl_junk_7"][:20]
    for s in merge_seed_specs:
        col = s.columns[0]["name"] if s.columns else "col"
        merged_entities.append(
            {
                "canonical_column": col,
                "source_columns": [col],
                "merge_confidence": 0.93,
                "strategies": ["glossary", "exact"],
                "citations": ["chaos seed"],
                "source_table": s.full_name,
            }
        )
    for s in specs[20:30]:
        col = s.columns[0]["name"] if s.columns else "legacy_col"
        review_candidates.append(
            {
                "legacy_name": f"legacy_{col}",
                "suggested_ddl": col,
                "merge_confidence": 0.65,
                "citation": "chaos review sample",
                "strategy": "vector",
                "source_table": s.full_name,
                "category": "review",
            }
        )
    for s in specs[30:35]:
        col = s.columns[0]["name"] if s.columns else "legacy_col"
        trash_candidates.append(
            {
                "legacy_name": f"trash_{col}",
                "suggested_ddl": col,
                "merge_confidence": 0.22,
                "citation": "chaos trash sample",
                "strategy": "noise",
                "source_table": s.full_name,
                "category": "trash",
            }
        )
    for s in specs:
        if s.full_name not in {"finance.mega_journal", "operations.wide_staging"}:
            continue
        for col in s.columns:
            merged_entities.append(
                {
                    "canonical_column": col["name"],
                    "source_columns": [col["name"]],
                    "merge_confidence": 0.93,
                    "strategies": ["glossary", "exact"],
                    "citations": ["chaos outlier width"],
                    "source_table": s.full_name,
                }
            )
    block: dict[str, Any] = {
        "merged_entities": merged_entities,
        "review_candidates": review_candidates,
        "trash_candidates": trash_candidates,
        "ddl_manifest": str(manifest_path.resolve()),
    }
    return block


def _build_inventory(specs: list[TableSpec]) -> list[dict[str, Any]]:
    inv: list[dict[str, Any]] = []
    for i, s in enumerate(specs):
        row: dict[str, Any] = {
            "full_name": s.full_name,
            "business_domain": s.business_domain,
            "query_count": s.query_count,
            "status": "active",
            "priority_score": round(((i % 10) + 1) / 10.0, 2),
            "column_count": len(s.columns),
            "description": f"Chaos dataset table {s.full_name}",
        }
        if s.sample_rows is not None:
            row["sample_rows"] = s.sample_rows
        inv.append(row)
    return inv


def _build_importance_ddl(specs: list[TableSpec]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for s in specs:
        for c in s.columns:
            out.append(
                {
                    "source_table": s.full_name,
                    "column": c["name"],
                    "data_type": c["type"],
                    "importance_score": 0.40 if "id" in c["name"] else 0.25,
                }
            )
    return out


def _write_sql_logs(path: Path, specs: list[TableSpec]) -> None:
    lines: list[str] = []
    for s in specs:
        for i in range(max(1, s.query_count)):
            lines.append(
                json.dumps(
                    {
                        "env": "chaos",
                        "dialect": "tsql",
                        "sql": f"SELECT * FROM {s.full_name} WHERE run_id = {i % 9}",
                    },
                    ensure_ascii=False,
                )
            )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_comms(path: Path, specs: list[TableSpec]) -> None:
    lines: list[str] = []
    for i, s in enumerate(specs[:120]):
        lines.append(
            json.dumps(
                {
                    "ts": f"2026-01-01T00:{i % 60:02d}:00Z",
                    "channel": "data-eng",
                    "user": f"user_{i % 7}",
                    "text": f"Reviewing {s.full_name} migration status in chaos run",
                },
                ensure_ascii=False,
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_readme(path: Path) -> None:
    rows = [
        ("finance.core_ledger", "RED", "none", "~90", "100", "Criticality override"),
        ("logistics.delivery_status", "GREEN", "INFO(null-rate skipped)", "~100", "~0", "High confidence, low impact"),
        ("finance.payment_staging", "RED", "none", "~85", ">=80", "Criticality wins over confidence band"),
        ("finance.invoice_attachments", "RED", "BLOCK(unsupported_blob_type)", "~60", "~10", "VARBINARY block"),
        ("legacy.document_archive", "RED", "BLOCK(unsupported_blob_type)", "~60", "~10", "NTEXT block"),
        ("sales.orders", "RED", "BLOCK(cluster_type_inconsistency)", "~85", "~20", "customer_id INT vs VARCHAR"),
        ("crm.orders", "RED", "BLOCK(cluster_type_inconsistency)", "~85", "~20", "customer_id VARCHAR vs INT"),
        ("finance.mega_journal", "YELLOW", "WARN(column_count_outlier)", "~75", "~20", "Extreme width"),
        ("operations.wide_staging", "YELLOW", "WARN(column_count_outlier)", "~75", "~20", "Moderate width"),
        ("technical_debt.tbl_junk_7", "RED", "INFO(null-rate skipped)", "0", "~0", "No glossary/type support"),
        ("operations.import_staging", "YELLOW", "WARN(high_null_rate)", "~80", "~20", "Sample rows with >80% NULL"),
    ]
    md = [
        "# Scale Engine Chaos Dataset",
        "",
        "Deterministic synthetic dataset for stress-testing Scale Engine scoring, anomalies, and queueing.",
        "",
        "| Table | Expected Queue | Anomaly Flags | Confidence | Criticality | Notes |",
        "|-------|----------------|---------------|------------|-------------|-------|",
    ]
    md.extend([f"| {a} | {b} | {c} | {d} | {e} | {f} |" for (a, b, c, d, e, f) in rows])
    path.write_text("\n".join(md) + "\n", encoding="utf-8")


def generate_chaos_dataset(*, out_dir: Path, seed: int = 42, tables: int = 100) -> dict[str, Any]:
    specs = _build_table_specs(total_tables=tables, seed=seed)
    out_dir.mkdir(parents=True, exist_ok=True)

    ddl_dir = out_dir / "ddl"
    ddl_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, str] = {"_comment": "Chaos manifest: schema.table -> DDL JSON path"}
    for s in specs:
        ddl_path = ddl_dir / f"{s.full_name.replace('.', '__')}.json"
        _write_json(ddl_path, {"columns": s.columns})
        manifest[s.full_name] = str(ddl_path.resolve())

    glossary = _build_glossary()
    glossary_path = out_dir / "chaos_glossary.json"
    manifest_path = out_dir / "chaos_manifest.json"
    _write_json(manifest_path, manifest)
    _write_json(glossary_path, glossary)
    _write_sql_logs(out_dir / "chaos_sql_logs.jsonl", specs)
    _write_comms(out_dir / "chaos_comms.jsonl", specs)
    _write_readme(out_dir / "README.md")

    report = {
        "schema_version": "1.2",
        "migration_context": "chaos.synthetic",
        "target_table": "chaos.synthetic",
        "queries_matched": int(sum(max(1, s.query_count) for s in specs)),
        "generated_at": "2026-01-01T00:00:00Z",
        "ingestion_stats": {
            "total_rows": int(sum(max(1, s.query_count) for s in specs)),
            "parse_ok": int(sum(max(1, s.query_count) for s in specs)),
            "regex_fallback": 0,
            "skipped_empty": 0,
        },
        "discovery": {"inventory": _build_inventory(specs)},
        "lineage": {"edges": _build_lineage(specs)},
        "alias_merge": _build_alias_merge(specs, glossary, manifest_path),
        "glossary_source": build_glossary_source_report(out_dir, [glossary_path]),
        "ddl_manifest_table_keys": sorted(k for k in manifest if not str(k).startswith("_")),
        "importance_ddl": _build_importance_ddl(specs),
    }
    # Validate full boundary shape before writing.
    AmaReportBoundarySchema.model_validate(report)
    _write_json(out_dir / "chaos_report.json", report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate deterministic Scale Engine chaos dataset.")
    parser.add_argument("--out", type=Path, required=True, help="Output directory")
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    parser.add_argument("--tables", type=int, default=100, help="Total number of tables (min 80)")
    args = parser.parse_args()
    generate_chaos_dataset(out_dir=args.out, seed=args.seed, tables=args.tables)
    print(f"Generated chaos dataset in: {args.out}")


if __name__ == "__main__":
    main()
