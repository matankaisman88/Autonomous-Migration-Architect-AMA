#!/usr/bin/env python3
"""
Multi-domain AMA fixture generator.

Produces DDL JSON, JSONL SQL logs (Hebrew/English bilingual), a candidate glossary,
comms JSONL, and Git SQL files for one of five business domains. Output lands in
``out/sandbox_{domain}_{timestamp}/`` so each run is isolated.

Usage:
  python tools/generate_domain_data.py --domain finance
  python tools/generate_domain_data.py --domain hr --lines 8000
  python tools/generate_domain_data.py --domain logistics --lines 12000 --seed 99
  python tools/generate_domain_data.py --domain retail
  python tools/generate_domain_data.py --domain healthcare

After generation, run the full pipeline:
  bash demo.sh --sandbox out/sandbox_hr_<timestamp>
  (or copy the printed ama-ingest command and run it directly)
"""

from __future__ import annotations

import argparse
import json
import random
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass
class TableSpec:
    schema: str
    table: str
    ddl_columns: list[str]
    hebrew_aliases: dict[str, str]
    is_legacy: bool = False
    is_temp: bool = False


@dataclass
class DomainVocabulary:
    """All fixtures for one business domain."""

    name: str
    display: str
    schemas: list[str]
    tables: list[TableSpec]
    glossary: dict[str, str]
    comms_messages: list[str]
    git_sql: dict[str, str]
    review_bands: list[str] = field(default_factory=list)


def _q(t: TableSpec) -> str:
    return f"{t.schema}.{t.table}"


def _ddl_fn(t: TableSpec) -> str:
    return f"{t.schema}_{t.table}.json".replace(".", "_")


def _join_key(a: TableSpec, b: TableSpec) -> str:
    ca, cb = set(a.ddl_columns), set(b.ddl_columns)
    for c in sorted(ca & cb):
        if c.endswith("_id"):
            return c
    return sorted(ca & cb)[0]


def _tbl(
    schema: str,
    table: str,
    cols: tuple[str, ...],
    aliases: dict[str, str],
    *,
    legacy: bool = False,
    temp: bool = False,
) -> TableSpec:
    return TableSpec(schema, table, list(cols), aliases, is_legacy=legacy, is_temp=temp)


def _rows_to_tables(rows: list[tuple[str, str, tuple[str, ...], dict[str, str], str]]) -> list[TableSpec]:
    out: list[TableSpec] = []
    for schema, table, cols, aliases, kind in rows:
        out.append(
            _tbl(schema, table, cols, aliases, legacy=kind == "L", temp=kind == "T"),
        )
    return out


def _reg() -> dict[str, DomainVocabulary]:
    finance = DomainVocabulary(
        "finance",
        "Finance",
        ["gl", "ar", "legacy_finance", "temp_finance"],
        _rows_to_tables(
            [
                ("gl", "journal_entries", ("entry_id", "account_id", "amount", "debit_amount", "credit_amount", "period", "posted_at", "currency", "created_by"), {"סכום": "amount", "חיוב": "debit_amount", "זיכוי": "credit_amount", "תקופה": "period", "מטבע": "currency"}, "N"),
                ("ar", "invoices", ("invoice_id", "account_id", "amount", "net_amount", "vat_amount", "due_date", "status", "issued_at"), {"חשבונית_ערך": "net_amount", "מועד_פירעון": "due_date", "סטטוס": "status"}, "N"),
                ("ar", "payments", ("payment_id", "invoice_id", "amount", "paid_at", "payment_method", "currency"), {"תשלום_סכום": "amount", "אמצעי_תשלום": "payment_method"}, "N"),
                ("legacy_finance", "חשבונות", (), {"חשבון": "account_id", "יתרה": "amount"}, "L"),
                ("temp_finance", "staging", ("id", "blob"), {}, "T"),
            ]
        ),
        {},
        [],
        {},
        ["invoiceid", "customerid", "entryid"],
    )
    hr = DomainVocabulary(
        "hr",
        "HR",
        ["hr", "payroll", "legacy_hr", "temp_hr"],
        _rows_to_tables(
            [
                ("hr", "employees", ("employee_id", "first_name", "last_name", "email", "department", "job_title", "hire_date", "is_active", "manager_id"), {"שם_פרטי": "first_name", "שם_משפחה": "last_name", "מחלקה": "department", "תפקיד": "job_title", "תאריך_גיוס": "hire_date", "פעיל": "is_active"}, "N"),
                ("hr", "departments", ("department_id", "name", "cost_center", "head_count", "budget_amount"), {"שם_מחלקה": "name", "תקציב": "budget_amount"}, "N"),
                ("payroll", "salary_records", ("record_id", "employee_id", "gross_amount", "net_amount", "tax_amount", "pay_period", "paid_at"), {"שכר_ברוטו": "gross_amount", "שכר_נטו": "net_amount", "מס": "tax_amount", "תקופת_שכר": "pay_period"}, "N"),
                ("legacy_hr", "עובדים_ישנים", (), {"עובד": "employee_id", "משכורת": "gross_amount"}, "L"),
                ("temp_hr", "import_staging", ("id", "raw"), {}, "T"),
            ]
        ),
        {},
        [],
        {},
        ["employeeid", "departmentid", "recordid"],
    )
    logistics = DomainVocabulary(
        "logistics",
        "Logistics",
        ["wms", "fleet", "legacy_wms", "temp_wms"],
        _rows_to_tables(
            [
                ("wms", "shipments", ("shipment_id", "order_id", "tracking_number", "carrier", "status", "shipped_at", "delivered_at", "warehouse_id", "weight_kg"), {"מספר_מעקב": "tracking_number", "ספק_משלוח": "carrier", "סטטוס": "status", "תאריך_משלוח": "shipped_at", "משקל": "weight_kg"}, "N"),
                ("wms", "inventory", ("item_id", "sku", "quantity", "reorder_level", "warehouse_id", "last_updated"), {"כמות": "quantity", "רמת_הזמנה": "reorder_level", "עדכון_אחרון": "last_updated"}, "N"),
                ("fleet", "vehicles", ("vehicle_id", "plate_number", "capacity_kg", "is_active", "assigned_route"), {"לוחית": "plate_number", "נפח": "capacity_kg", "פעיל": "is_active"}, "N"),
                ("legacy_wms", "משלוחים_ישנים", (), {"משלוח": "shipment_id", "סטטוס_ישן": "status"}, "L"),
                ("temp_wms", "route_staging", ("id", "raw"), {}, "T"),
            ]
        ),
        {},
        [],
        {},
        ["shipmentid", "warehouseid", "itemid"],
    )
    retail = DomainVocabulary(
        "retail",
        "Retail",
        ["catalog", "pos", "legacy_retail", "temp_retail"],
        _rows_to_tables(
            [
                ("catalog", "products", ("product_id", "sku", "name", "category", "unit_price", "stock_qty", "is_active", "supplier_id"), {"שם_מוצר": "name", "קטגוריה": "category", "מחיר": "unit_price", "מלאי": "stock_qty", "פעיל": "is_active"}, "N"),
                ("pos", "transactions", ("transaction_id", "product_id", "quantity", "unit_price", "discount", "total_amount", "sold_at", "cashier_id"), {"כמות": "quantity", "הנחה": "discount", "סה_כ": "total_amount", "תאריך_מכירה": "sold_at"}, "N"),
                ("pos", "returns", ("return_id", "transaction_id", "quantity", "reason", "refund_amount", "returned_at"), {"סיבה": "reason", "סכום_החזר": "refund_amount"}, "N"),
                ("legacy_retail", "מוצרים_ישנים", (), {"פריט": "product_id", "מחיר_ישן": "unit_price"}, "L"),
                ("temp_retail", "import_queue", ("id", "raw"), {}, "T"),
            ]
        ),
        {},
        [],
        {},
        ["productid", "transactionid", "returnid"],
    )
    healthcare = DomainVocabulary(
        "healthcare",
        "Healthcare",
        ["clinical", "billing", "legacy_clinical", "temp_clinical"],
        _rows_to_tables(
            [
                ("clinical", "patients", ("patient_id", "first_name", "last_name", "birth_date", "gender", "phone", "email", "is_active"), {"שם_פרטי": "first_name", "שם_משפחה": "last_name", "תאריך_לידה": "birth_date", "מין": "gender", "טלפון": "phone"}, "N"),
                ("clinical", "visits", ("visit_id", "patient_id", "doctor_id", "visit_date", "diagnosis", "notes", "status"), {"תאריך_ביקור": "visit_date", "אבחנה": "diagnosis", "הערות": "notes", "סטטוס": "status"}, "N"),
                ("billing", "charges", ("charge_id", "visit_id", "amount", "insurance_amount", "patient_amount", "status", "billed_at"), {"חיוב": "amount", "סכום_ביטוח": "insurance_amount", "סכום_מטופל": "patient_amount"}, "N"),
                ("legacy_clinical", "רשומות_ישנות", (), {"מטופל": "patient_id", "סכום_חיוב": "amount"}, "L"),
                ("temp_clinical", "import_staging", ("id", "raw"), {}, "T"),
            ]
        ),
        {},
        [],
        {},
        ["patientid", "visitid", "chargeid"],
    )
    for d in (finance, hr, logistics, retail, healthcare):
        d.glossary = {k: v for t in d.tables for k, v in t.hebrew_aliases.items()}
        leg = next(t for t in d.tables if t.is_legacy)
        t0, t1 = [t for t in d.tables if not t.is_legacy and not t.is_temp][:2]
        jk = _join_key(t0, t1)
        d.git_sql = {
            f"reports/{d.name}_summary.sql": (
                f"-- {d.display} summary\nSELECT t1.{t0.ddl_columns[0]}, COUNT(*) cnt\n"
                f"FROM {_q(t0)} t1 JOIN {_q(t1)} t2 ON t1.{jk}=t2.{jk}\nGROUP BY t1.{t0.ddl_columns[0]};\n"
            ),
            f"reports/{d.name}_detail.sql": f"-- detail\nSELECT * FROM {_q(t0)} WHERE {t0.ddl_columns[0]}>0;\n",
            f"legacy/{d.name}_bridge.sql": (
                f"-- legacy bridge\nSELECT h.[{next(iter(leg.hebrew_aliases))}],t.{t0.ddl_columns[0]}\n"
                f"FROM {_q(leg)} h LEFT JOIN {_q(t0)} t ON 1=1;\n"
            ),
        }
        names = [f"{t.schema}.{t.table}" for t in d.tables if not t.is_temp]
        d.comms_messages = [
            f"{d.display}: migrate {names[0]} before dependents (#{i})." for i in range(30)
        ]
    return {"finance": finance, "hr": hr, "logistics": logistics, "retail": retail, "healthcare": healthcare}


_DOMAIN_REGISTRY = _reg()


class DomainFactory:
    """Generates a complete AMA sandbox for one domain."""

    def __init__(self, domain: str, *, seed: int = 42) -> None:
        self.vocab: DomainVocabulary = _DOMAIN_REGISTRY[domain]
        self.rng = random.Random(seed)

    def generate(self, *, n_lines: int = 10000, out_parent: Path) -> Path:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        sandbox = (out_parent / f"sandbox_{self.vocab.name}_{ts}").resolve()
        sandbox.mkdir(parents=True, exist_ok=True)
        self._ddl(sandbox)
        self._logs(sandbox, n_lines)
        self._gloss(sandbox)
        self._comms(sandbox)
        self._git(sandbox)
        self._readme(sandbox, n_lines)
        return sandbox

    def _ddl(self, sb: Path) -> None:
        dd = sb / "ddl"
        dd.mkdir(parents=True, exist_ok=True)
        man: dict[str, str] = {"_comment": f"{self.vocab.display} manifest"}
        for t in self.vocab.tables:
            if t.is_temp:
                continue
            fn = _ddl_fn(t)
            rel = f"ddl/{fn}"
            cols = t.ddl_columns if t.ddl_columns else list(t.hebrew_aliases)
            (dd / fn).write_text(json.dumps({"columns": cols}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            man[f"{t.schema}.{t.table}"] = rel
        (dd / "manifest.json").write_text(json.dumps(man, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def _join_sql(self, a: TableSpec, b: TableSpec) -> str:
        n = self.rng.randint(1, 50000)
        j = _join_key(a, b)
        ha = next(iter(a.hebrew_aliases))
        hb = next(iter(b.hebrew_aliases))
        return (
            f"SELECT t1.{a.ddl_columns[0]}, t1.[{ha}], t2.{b.ddl_columns[0]}, t2.[{hb}] "
            f"FROM {_q(a)} t1 INNER JOIN {_q(b)} t2 ON t1.{j} = t2.{j} WHERE t1.{a.ddl_columns[0]} = {n}"
        )

    def _single(self, t: TableSpec) -> str:
        n = self.rng.randint(1, 90000)
        if t.is_legacy:
            return f"SELECT {', '.join(f'[{h}]' for h in t.hebrew_aliases)} FROM {_q(t)} WHERE 1=1"
        if t.is_temp:
            return f"SELECT * FROM {_q(t)} WHERE id = {n}"
        bits = [*(t.ddl_columns[:3]), *[f'[{h}]' for h in list(t.hebrew_aliases)[:2]]]
        return f"SELECT {', '.join(bits)} FROM {_q(t)} WHERE {t.ddl_columns[0]} = {n}"

    def _biling(self, t: TableSpec) -> str:
        elig = [x for x in self.vocab.tables if x.hebrew_aliases and not x.is_legacy and not x.is_temp]
        t = t if (t.hebrew_aliases and not t.is_legacy and not t.is_temp) else elig[0]
        it = list(t.hebrew_aliases.items())[:2]
        if len(it) < 2:
            h, e = it[0]
            return f"SELECT {t.ddl_columns[0]}, [{h}], {e} FROM {_q(t)} WHERE {t.ddl_columns[0]} > 0"
        (h1, e1), (h2, e2) = it[0], it[1]
        return f"SELECT {t.ddl_columns[0]}, [{h1}], {e1}, [{h2}], {e2} FROM {_q(t)} WHERE {t.ddl_columns[0]} > 0"

    def _rev(self) -> str:
        rb = self.vocab.review_bands[self.rng.randint(0, len(self.vocab.review_bands) - 1)]
        t = next(tb for tb in self.vocab.tables if not tb.is_legacy and not tb.is_temp)
        return f"SELECT {rb}, {t.ddl_columns[1]}, {t.ddl_columns[2]} FROM {_q(t)} WHERE {rb} > 0"

    def _logs(self, sb: Path, n: int) -> None:
        elig = [t for t in self.vocab.tables if not t.is_legacy and not t.is_temp]
        t0, t1 = elig[0], elig[1]
        jn = min(max(0, round(n * 0.25)), n)
        rem = n - jn
        bn = min(max(0, round(n * 0.01)), rem)
        rem -= bn
        rn = min(max(0, round(n * 0.01)), rem)
        sn = n - jn - bn - rn
        rows: list[dict[str, str]] = [{"env": "prod", "dialect": "tsql", "sql": self._join_sql(t0, t1)} for _ in range(jn)]
        pr = [t for t in elig if t.hebrew_aliases]
        for i in range(bn):
            rows.append({"env": "prod", "dialect": "tsql", "sql": self._biling(pr[i % len(pr)])})
        for _ in range(rn):
            rows.append({"env": "prod", "dialect": "tsql", "sql": self._rev()})
        pool = [t for t in self.vocab.tables if not t.is_legacy and not t.is_temp]
        for i in range(sn):
            rows.append({"env": "prod", "dialect": "tsql", "sql": self._single(pool[i % len(pool)])})
        self.rng.shuffle(rows)
        sd = sb / "sql_logs"
        sd.mkdir(parents=True, exist_ok=True)
        (sd / f"{self.vocab.name}_prod.jsonl").write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8"
        )

    def _gloss(self, sb: Path) -> None:
        g = sb / "glossary"
        g.mkdir(parents=True, exist_ok=True)
        v = self.vocab.name
        clean = dict(sorted(self.vocab.glossary.items()))
        (g / f"{v}_glossary.json").write_text(json.dumps(clean, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        keys = list(clean.keys())
        dirty = {k[:2]: clean[k] for k in keys[: min(8, len(keys))]}
        (g / f"{v}_glossary_dirty.json").write_text(json.dumps(dirty, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def _comms(self, sb: Path) -> None:
        c = sb / "comms"
        c.mkdir(parents=True, exist_ok=True)
        lines = [
            json.dumps({"channel": "migration", "ts": f"1704067200.{i:06d}", "text": tx}, ensure_ascii=False)
            for i, tx in enumerate(self.vocab.comms_messages)
        ]
        (c / f"{self.vocab.name}_slack.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _git(self, sb: Path) -> None:
        gr = sb / "git_sql"
        for rel, body in self.vocab.git_sql.items():
            p = gr / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body, encoding="utf-8")

    def _readme(self, sb: Path, n_lines: int) -> None:
        v = self.vocab
        tls = "\n".join(
            f"- `{_q(t)}` — {'legacy' if t.is_legacy else 'temp' if t.is_temp else 'active'} — "
            f"{', '.join(list(t.hebrew_aliases)[:2])}{'…' if len(t.hebrew_aliases) > 2 else ''}"
            for t in v.tables
        )
        t0, t1 = [t for t in v.tables if not t.is_legacy and not t.is_temp][:2]
        (sb / "README.md").write_text(
            f"# {v.display} sandbox ({v.name})\n\n"
            f"Fictional {v.display} org migrating mixed Hebrew/English SQL; AMA inventories assets and waves.\n\n"
            f"## Tables\n\n{tls}\n\n## Quickstart\n\n```bash\n"
            f"python tools/generate_domain_data.py --domain {v.name} --lines {n_lines}\n\n"
            f"ama-ingest run \\\n  --data-root . \\\n  --sql-logs \"{sb.name}/sql_logs/{v.name}_prod.jsonl\" \\\n"
            f"  --ddl-manifest \"{sb.name}/ddl/manifest.json\" \\\n"
            f"  --glossary \"{sb.name}/glossary/{v.name}_glossary.json\" \\\n"
            f"  --glossary-dirty \"{sb.name}/glossary/{v.name}_glossary_dirty.json\" \\\n"
            f"  --comms-dir \"{sb.name}/comms\" \\\n  --git-sql-roots \"{sb.name}/git_sql\" \\\n"
            f"  --target-schema dbo --target-table orders \\\n  --discovery-mode --discovery-merge-all \\\n  --format json -o report.json\n```\n\n"
            f"Or: `bash demo.sh --sandbox {sb}`\n\n## What to look for\n\n"
            f"- Review: {', '.join(v.review_bands)}.\n"
            f"- Bilingual probes → glossary co-occurrence.\n"
            f"- Planner: `{_q(t0)}` ↔ `{_q(t1)}` JOINs.\n",
            encoding="utf-8",
        )


def main() -> None:
    p = argparse.ArgumentParser(description="Generate AMA demo fixtures for a business domain.")
    p.add_argument("--domain", choices=["finance", "hr", "logistics", "retail", "healthcare"], required=True)
    p.add_argument("--lines", type=int, default=10000, help="SQL log row count (default: 10000)")
    p.add_argument("--seed", type=int, default=42, help="Random seed (default: 42)")
    p.add_argument("--out-dir", type=str, default="out", help="Parent output directory (default: out/)")
    args = p.parse_args()
    factory = DomainFactory(args.domain, seed=args.seed)
    sandbox = factory.generate(n_lines=args.lines, out_parent=Path(args.out_dir))
    print(f"\nSandbox: {sandbox}")
    print("\nQuickstart:")
    print(f"  bash demo.sh --sandbox {sandbox}")


if __name__ == "__main__":
    main()
