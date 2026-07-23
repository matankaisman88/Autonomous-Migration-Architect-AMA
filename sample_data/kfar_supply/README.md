> **Dev/test fixture only.** Kfar Supply is not part of the production flow. It exists so developers can exercise AMA without a real company database.

# Kfar Supply demo dataset

## What is Kfar Supply?

Kfar Supply Ltd. is a fictional Israeli wholesale distributor used  
as a controlled AMA demo. The company runs a twelve-year-old SQL Server database that  
mixes Hebrew business column labels with English DDL from ERP upgrades. The team is  
migrating to **Azure Synapse** and needs an evidence-backed inventory, alias mapping,
and wave plan without building spreadsheets by hand.

A legacy Hebrew billing table, **`legacy_hebrew.חשבוניות`**, still feeds finance
reconciliation. Staging debris such as **`temp_junk.Tmp_staging`** sits outside the
official DDL manifest so discovery can show “seen in logs but not in DDL scope.”

## What does this demo show?

- **Hebrew ↔ English alias resolution** via `kfar_glossary.json` (glossary tier) and
English DDL columns (exact tier), plus **review-band** typos (`customerid`, `qty`,
`paydt`, …) with `kfar_glossary_dirty.json`.
- **Six-schema style discovery**: `dbo`, `finance`, `logistics`, `legacy_hebrew`,
`temp_junk`, plus cross-schema references in one coherent narrative.
- **Cross-schema lineage** in the SQL log (orders ↔ invoices, orders ↔ lines,
orders ↔ shipments, invoices ↔ payments, customers ↔ orders) for the planner and
graph views.
- **HITL review queue**: expect on the order of **3–5** ambiguous mappings in the
0.4–0.8 confidence band, depending on thresholds.
- **Domain clustering** cues: Operations (`dbo.orders`, `dbo.order_lines`),
Finance (`finance.invoices`, `finance.payments`), Logistics (`logistics.shipments`).
- **Planner topo-sort**: Finance work tends to order **invoices before payments**
when lineage edges are present.

## Quickstart

From the **repository root** (after `pip install -e .`), run **`bash demo.sh`** to execute the full demo in one step: regenerate sample data, ingest with discovery + `--discovery-merge-all`, export the migration plan to **Jira CSV** (import-ready) and Confluence HTML, and print the output file paths. The script lives at the repo root as `demo.sh`, not under this folder.

The block below is the same pipeline **step by step** (manual commands).

```bash
# Step 0 (optional): Auto-generate a glossary if you don't have one
ama-ingest generate-glossary sample_data/kfar_supply/sql_logs/kfar_prod.jsonl --ddl-columns sample_data/kfar_supply/ddl/dbo_orders.json --ddl-manifest sample_data/kfar_supply/ddl/kfar_manifest.json --out candidate_glossary.json --min-count 3

# Review candidate_glossary.json, then use it as your glossary:
# --glossary candidate_glossary.json
# (replaces --glossary sample_data/kfar_supply/glossary/kfar_glossary.json)

# 1. Generate the dataset (already done if files exist)
python tools/generate_kfar_supply.py

# 2. Run full discovery ingestion (one line — safe for PowerShell; Bash may use \ at EOL)
ama-ingest run \
  --data-root . \
  --sql-logs "sample_data/kfar_supply/sql_logs/kfar_prod.jsonl" \
  --ddl-columns sample_data/kfar_supply/ddl/dbo_orders.json \
  --ddl-manifest sample_data/kfar_supply/ddl/kfar_manifest.json \
  --glossary sample_data/kfar_supply/glossary/kfar_glossary.json \
  --glossary-dirty sample_data/kfar_supply/glossary/kfar_glossary_dirty.json \
  --comms-dir sample_data/kfar_supply/comms \
  --git-sql-roots sample_data/kfar_supply/git_sql \
  --target-schema dbo --target-table orders \
  --discovery-mode --discovery-merge-all \
  --format json -o sample_data/kfar_supply/kfar_report.json

# 3. View dashboard
ama-dashboard --report-path sample_data/kfar_supply/kfar_report.json

# 4. Export migration plan to Jira format
ama-ingest export-plan --report sample_data/kfar_supply/kfar_report.json --format jira --out sample_data/kfar_supply/kfar_export_jira.csv

# 5. Export to Confluence
ama-ingest export-plan --report sample_data/kfar_supply/kfar_report.json --format confluence --out sample_data/kfar_supply/kfar_export_confluence.html
```

## What to look for in the report


| Area                  | Where to look                                                                                          |
| --------------------- | ------------------------------------------------------------------------------------------------------ |
| **Business Glossary** | Dashboard **Business Glossary** tab — Hebrew → English with layer + confidence (e.g. סכום → `amount`). |
| **HITL**              | **Review (HITL)** tab — 3–4 review candidates across multiple schemas (`customerid`, `invoiceid`, `shipmentid`) — each is a compound DDL column name with the underscore removed, landing in the 0.40–0.80 confidence review band. |
| **Planner**           | **Planner** tab — Finance wave ordering (invoices before payments when lineage applies).               |
| **Lineage**           | **Tables** tab — graph edges linking `dbo.orders`, `finance.invoices`, `finance.payments`.             |


## Schema summary


| Table         | Schema          | DDL manifest | Expected inventory note      | Domain           |
| ------------- | --------------- | ------------ | ---------------------------- | ---------------- |
| `orders`      | `dbo`           | Mapped       | In DDL + high log volume     | Operations       |
| `order_lines` | `dbo`           | Mapped       | In DDL                       | Operations       |
| `customers`   | `dbo`           | Mapped       | In DDL                       | Operations       |
| `invoices`    | `finance`       | Mapped       | In DDL                       | Finance          |
| `payments`    | `finance`       | Mapped       | In DDL                       | Finance          |
| `shipments`   | `logistics`     | Mapped       | In DDL                       | Logistics        |
| `חשבוניות`    | `legacy_hebrew` | Unmapped     | Discovered, outside manifest | Finance / legacy |
| `Tmp_staging` | `temp_junk`     | Unmapped     | Technical debt / staging     | Unclassified     |


## What if I don't have a glossary?

Run `ama-ingest generate-glossary` (see Quickstart Step 0) before the main ingestion.
AMA mines co-occurrences between Hebrew/RTL column names and English DDL columns
that appear in the same SQL queries — no manual effort required.

| Method | Confidence | Requires |
| --- | --- | --- |
| Co-occurrence | 0.30–0.95 | SQL logs + DDL file |
| LLM translation | 0.50–0.95 | API key + co-occurrence as fallback |

Review `candidate_glossary.json` before using it. The `_meta.candidates` block
shows how each mapping was derived and its confidence score.

> The live API performs read-only `real_extract` only — there is no bundled demo upload mode.

## Using this fixture

Two ways to work with the fixture during local development:

| Approach | When to use |
| --- | --- |
| **File-based** (this folder + `demo.sh` / `ama-ingest run`) | Reproducible offline run, CI, Hebrew glossary + comms + git SQL from `sample_data/kfar_supply` |
| **Local SQL Server + Live connection** | Set `MSSQL_SA_PASSWORD`, run `python tools/setup_dev_mssql.py`, then use **Live connection** — see [docs/LIVE_CONNECTION.md](../../docs/LIVE_CONNECTION.md) and [docs/SQLSERVER.md](../../docs/SQLSERVER.md) |

`demo.sh` is bash-only. On Windows, run the equivalent `ama-ingest` commands from the Quickstart section below, or use the Live connection path above.

For Query Store testing against the local fixture database, run [`tools/kfar_test_queries.sql`](../../tools/kfar_test_queries.sql) in SSMS after loading it.
