# Autonomous Migration Architect (AMA)

**Turn legacy SQL logs into an evidence-backed cloud migration plan — automatically.**

AMA reads your SQL logs, resolves column aliases (including Hebrew ↔ English), discovers
your full table inventory, and produces wave-by-wave migration plans with business rationale.
No manual inventory. No spreadsheets. Repeatable from day one.


|            |                                                                                                                    |
| ---------- | ------------------------------------------------------------------------------------------------------------------ |
| **Input**  | SQL log JSONL files, DDL JSON, Slack/comms export, Git SQL                                                         |
| **Output** | JSON report · Excel workbook · Streamlit dashboard · Jira CSV import · Jira bulk JSON (optional) · Confluence page |
| **Python** | 3.11+ · [MIT License](LICENSE)                                                                                     |


## Features (high level)

- **Discovery + lineage** — Multi-schema inventory, domain clustering, and an undirected co-query lineage graph (bare schema-name tokens from the parser are filtered so edges only connect real `schema.table` keys).
- **Broken lineage** — Tables referenced in SQL that are not listed in the DDL manifest are flagged in the JSON report (`lineage.broken_table_keys`, `ddl_manifest_table_keys`), surfaced as **warnings** on ingest (exit code 0), and shown in the **Planner** (`is_broken`, `missing_parents`, review wave for manifest gaps) and **Tables** tab lineage graph (diamond nodes, warning tooltip).
- **Migration Agent (chat-first AI cockpit)** — Goal-oriented dbt migration with a human approval gate (`request_write_permission`), structured intelligence feed tables, per-wave progress tracking, automatic dbt test + fix loop, synthetic sample fallback when source DuckDB tables are unavailable, and confidence-aware mapping review.
- **Dashboard KPI alignment** — Executive **% Confirmed** uses merge rows whose `source_table` appears in the **filtered inventory** (same scope as the Domains tab and table list), so percentages stay consistent with sidebar filters.
- **Alias merge scope** — Default single-table merge for the migration anchor, or `**--discovery-merge-all`** to run the four-tier resolver against every discovered table in DDL scope (recommended for the Kfar demo so review-band candidates appear across schemas).
- **Exports** — `**ama-ingest export-plan`** defaults to **Jira CSV** (one Task per inventory row, `utf-8-sig`, `csv.QUOTE_ALL`, one-line flattened **Description**, no Project Key column — pick the project in Jira’s import UI). Use `**--format jira-json`** for Jira Cloud bulk-create JSON (epics/stories, ADF). **Confluence** is wiki storage HTML. Inline Markdown in wave rationales maps to Jira ADF / HTML where applicable.
- **Glossary tooling** — `**ama-ingest generate-glossary`** mines Hebrew/RTL ↔ English co-occurrences from SQL logs into a candidate glossary JSON (optional LLM assist).
- **One-command demo** — Repository root scripts: `**demo.sh`** (Bash/Git Bash) and `**demo.ps1**` (PowerShell/Windows): regenerate Kfar data → ingest with discovery + merge-all → Jira + Confluence exports → prints output paths.
- **Multi-domain fixtures** — `**tools/generate_domain_data.py`** builds a full AMA sandbox (DDL, JSONL logs, glossary, comms, Git SQL, README) for `**finance**`, `**hr**`, `**logistics**`, `**retail**`, or `**healthcare**`, under `**out/sandbox_{domain}_YYYYMMDD_HHMMSS/**`. SQL logs include varied patterns (multi-way joins, CTEs, self-joins, and a small share of **broken lineage** joins to fictional `ghost_system.*` tables for integration testing). One command: `**bash demo.sh --domain hr`** (Bash/Git Bash) or `**./demo.ps1 -Domain hr**` (PowerShell) — both generate the sandbox, then run ingest + exports. Or generate only, then run with `**--sandbox**` / `**-Sandbox**` using the printed path (no new dependencies).

## Installation

Clone and enter the repository:

```bash
git clone <repository-url>
cd Autonomous-Migration-Architect-AMA
```

### Quick install (recommended)

**Windows (PowerShell):**

```powershell
powershell -ExecutionPolicy Bypass -File scripts/install.ps1
# Optional extras: -Embed, -Viz, or -All
```

**macOS / Linux / Git Bash:**

```bash
bash scripts/install.sh
# Optional extras: --embed, --viz, or --all
```

Create a `**.env**` file in the working directory (optional) with `AMA_*` variables — loaded automatically by `IngestionSettings`.

## Demo (30 seconds)

> **Dataset:** Kfar Supply Ltd. — fictional Israeli wholesale distributor,
> 8-table SQL Server legacy database, migrating to Azure Synapse.

**Option A — full toolchain in one script:**

```bash
bash demo.sh
```

```powershell
.\demo.ps1
```

If PowerShell blocks script execution on Windows, run:

```powershell
powershell -ExecutionPolicy Bypass -File .\demo.ps1
```

**Option B — same steps manually (PowerShell-friendly one-liners):**

```bash
python tools/generate_kfar_supply.py
ama-ingest run --sql-logs "sample_data/kfar_supply/sql_logs/*.jsonl" --ddl-manifest sample_data/kfar_supply/ddl/kfar_manifest.json --glossary sample_data/kfar_supply/glossary/kfar_glossary.json --glossary-dirty sample_data/kfar_supply/glossary/kfar_glossary_dirty.json --comms-dir sample_data/kfar_supply/comms --git-sql-roots sample_data/kfar_supply/git_sql --target-schema dbo --target-table orders --discovery-mode --discovery-merge-all --format json -o sample_data/kfar_supply/kfar_report.json
ama-ingest export-plan --report sample_data/kfar_supply/kfar_report.json --format jira --out sample_data/kfar_supply/kfar_export_jira.csv
ama-ingest export-plan --report sample_data/kfar_supply/kfar_report.json --format confluence --out sample_data/kfar_supply/kfar_export_confluence.html
ama-dashboard --report-path sample_data/kfar_supply/kfar_report.json
```

See `**sample_data/kfar_supply/README.md**` for a step-by-step quickstart (optional `generate-glossary`, explicit log path, and `--ddl-columns`).

### Multi-domain sandbox (optional)

Pass `**--domain**`/`**-Domain**` to `**demo.sh**` / `**demo.ps1**` or use `**tools/generate_domain_data.py**` directly with one of these values:


| `--domain`       | Vertical (fixture story)                                                   |
| ---------------- | -------------------------------------------------------------------------- |
| `**finance**`    | General ledger + accounts receivable (journal entries, invoices, payments) |
| `**hr**`         | HR + payroll (employees, departments, salary records)                      |
| `**logistics**`  | WMS + fleet (shipments, inventory, vehicles)                               |
| `**retail**`     | Catalog + POS (products, transactions, returns)                            |
| `**healthcare**` | Clinical + billing (patients, visits, charges)                             |


**Single command** (generate sandbox + full pipeline):

```bash
bash demo.sh --domain hr
# Optional: bash demo.sh --domain finance --lines 8000 --seed 99
```

```powershell
.\demo.ps1 -Domain hr
# Optional: .\demo.ps1 -Domain finance -Lines 8000 -Seed 99
```

**Two steps** (generate only, then ingest an existing folder):

```bash
python tools/generate_domain_data.py --domain hr --lines 10000 --seed 42
bash demo.sh --sandbox out/sandbox_hr_YYYYMMDD_HHMMSS   # exact path printed by the generator
```

```powershell
python tools/generate_domain_data.py --domain hr --lines 10000 --seed 42
.\demo.ps1 -Sandbox out/sandbox_hr_YYYYMMDD_HHMMSS      # exact path printed by the generator
```

Each run writes a timestamped directory under `**out/**` (gitignored) with `**ddl/manifest.json**`, `**sql_logs/{domain}_prod.jsonl**`, `**glossary/{domain}_glossary*.json**`, `**comms/**`, and `**git_sql/**`.

`**demo.sh` / `demo.ps1**` write the JSON report and exports **inside the active sandbox**: `**sample_data/kfar_supply/kfar_*.json`** (and HTML) for the default Kfar run, and `**out/sandbox_{domain}_…/{domain}_report.json**` (plus matching Jira/Confluence names) for `**--domain**` / `**-Domain**`.

**Dashboard “business domain” vs `--domain`:** The `**--domain`** flag only picks which **fixture pack** to generate (finance, hr, …). The AMA report still classifies each table into the **fixed portfolio taxonomy** used everywhere in the product: Finance, Logistics, CRM, Marketing, Analytics, Operations, Legacy Core, Technical Debt. So `**--domain healthcare`** does not create a “Healthcare” bucket in the UI; clinical/billing tables are mapped into those portfolios (for example clinical → CRM, billing → Finance). Regenerate the report after upgrading AMA if you rely on these labels.

**What you'll see in the dashboard:**

- Executive overview: 6 domains, risk hotspots, impact vs readiness scatter
- Planner tab: migration waves (Finance: invoices → payments ordered by lineage)
- Business Glossary: Hebrew ↔ English column mappings with confidence scores
- HITL Review: typically **3–4** ambiguous mappings across schemas when using `**--discovery-merge-all`** (compound DDL names without underscores, review band ~0.4–0.8)
- Tables tab: interactive lineage graph (dbo.orders is the hub)

## How It Works

**Step 1 — Ingest.** AMA streams SQL JSONL logs through a SQLGlot parser (with regex fallback), extracting table references and column usage patterns. Every record is sanitized (NFC normalization, null-byte stripping, RTL display handling for Hebrew identifiers).

**Step 2 — Resolve.** Each column seen in logs is matched to your target DDL through a four-tier alias pipeline: glossary lookup → exact normalized match → lexical near-miss → hash-embedding vector similarity. Confidence scores drive three buckets: merged (≥0.8), review queue (0.4–0.8), and discarded (<0.4).

**Step 3 — Discover.** In discovery mode, AMA inventories every `schema.table` seen across all logs, scores each by query volume (priority score = % of max), classifies business domains, detects technical debt, builds a co-query lineage graph, and generates executive risk hotspots.

**Step 4 — Plan.** The Autonomous Planner groups tables by business domain, orders them using Kahn's topological sort over the lineage graph (dependency-safe wave ordering), and annotates each wave with business rationale and technical notes drawn from the report data.

## Metadata Confidence & Coverage

- **Analysis quality scales with query log density.** AMA surfaces uncertainty rather than guessing.
- **Logic vs. Schema.** AMA maps physical data relationships from query logs. Dynamic SQL and stored procedures are out of scope by design.
- **Sparsity.** Low-volume tables produce thin co-occurrence signal, lower confidence scores, and are routed automatically to HITL review.

> ⚠️ **Table missing from your migration plan?** It likely has a sparse-log problem. Review it manually to ensure complete coverage.

## Architecture


| Package / module     | Role                                                                                                                        |
| -------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| `ama.parsing`        | SQLGlot-backed SQL parse + regex fallback                                                                                   |
| `ama.sql_pipeline`   | Streaming JSONL ingestion, telemetry, lineage accumulation                                                                  |
| `ama.lineage`        | Undirected co-query graph (`LineageGraph`); edges use real `schema.table` keys only                                         |
| `ama.alias_resolver` | 4-tier column alias resolution (glossary → exact → lexical → vector)                                                        |
| `ama.discovery`      | Multi-schema inventory, priority scoring, domain classification                                                             |
| `ama.business_logic` | Executive narrative, domain clustering, impact/readiness scatter                                                            |
| `ama.planner`        | Migration waves: Kahn topo-sort over lineage graph + business rationales; **broken lineage** metadata (`broken_lineage.py`) |
| `ama.export`         | Jira CSV (default), Jira bulk JSON, Confluence HTML; Markdown ** / ``` → ADF / HTML for JSON export                         |
| `ama.glossary`       | Co-occurrence mining + optional LLM translation for candidate glossaries                                                    |
| `ama.data_quality`   | DQ suite: boundary validation, schema version, ingestion stats checks                                                       |
| `ama.log_analysis`   | Streaming log scan facade (no full ingest — telemetry only)                                                                 |
| `ama.security`       | Path redaction, secret masking, path-traversal guard                                                                        |
| `ama.schemas`        | Pydantic report contracts (`AmaReportBoundarySchema`, schema version)                                                       |
| `ama.ui`             | Streamlit dashboard (9 tabs, including Migration Agent)                                                                    |


## CLI Reference


| Command                                                           | What it does                                                                    |
| ----------------------------------------------------------------- | ------------------------------------------------------------------------------- |
| `ama-ingest run`                                                  | Full ingestion: SQL logs + comms + Git + importance scoring                     |
| `ama-ingest run --discovery-mode`                                 | All of `run` + full multi-schema discovery inventory + lineage graph            |
| `ama-ingest run --format excel -o report.xlsx`                    | Excel workbook output                                                           |
| `ama-ingest dq --report report.json`                              | Data quality checks (boundary, schema version, ingestion stats)                 |
| `ama-ingest plan --report report.json`                            | Migration plan JSON from discovery inventory                                    |
| `ama-ingest export-plan --report report.json`                     | Jira **CSV** import file (default `--format jira`; one row per inventory table) |
| `ama-ingest export-plan --report report.json --format jira-json`  | Jira bulk-create **JSON** (epics + stories per wave, ADF)                       |
| `ama-ingest export-plan --report report.json --format confluence` | Confluence wiki storage HTML                                                    |
| `ama-ingest generate-glossary …`                                  | Mine SQL logs + DDL → candidate glossary JSON (see `--help`)                    |
| `ama-ingest generate-dbt`                                         | Generate dbt models from AMA report → see [MIGRATION.md](./MIGRATION.md)        |
| `ama-ingest log-scan PATH [PATH...]`                              | Streaming log scan → parse telemetry JSON (no full report)                      |
| `ama-ingest apply-hitl --report report.json`                      | Apply HITL sidecar decisions → merged report                                    |
| `ama-ingest run --benchmark`                                      | Performance benchmark → `benchmark_results.json`                                |
| `ama-dashboard --report-path report.json`                         | Launch Streamlit dashboard                                                      |


## Jira & Confluence (Atlassian)

AMA does **not** call Jira or Confluence APIs directly. It writes **files** you import into your workspace.


| Target               | Command                                                                                      | Output                                                                                                                                                                                                                                                                                           | Typical use                                                                                      |
| -------------------- | -------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------ |
| **Jira (default)**   | `ama-ingest export-plan --report report.json --out plan_jira.csv`                            | **CSV** (`utf-8-sig`, **QUOTE_ALL**, no Project Key column): **Summary** (`Migrate: schema.table`), **Task**, **Priority**, flat one-line **Description** (no embedded newlines), **Labels** (single sanitized tag). Assign project in Jira UI on import. Same as `tools/report_to_jira_csv.py`. |                                                                                                  |
| **Jira (bulk JSON)** | `ama-ingest export-plan --report report.json --format jira-json --out plan_jira.json`        | Bulk-create JSON: epics and stories per migration wave, **ADF** issue bodies                                                                                                                                                                                                                     | Jira Cloud **Import issues** (JSON) or REST automation.                                          |
| **Confluence**       | `ama-ingest export-plan --report report.json --format confluence --out plan_confluence.html` | Wiki **storage** HTML (migration plan narrative: waves, tables, rationales)                                                                                                                                                                                                                      | Create a page in Confluence and **insert** or **import** the HTML, or host the file and link it. |


**Prerequisites:** A completed `**ama-ingest run`** JSON report (with `**--discovery-mode**` so the inventory exists). `**demo.sh**` / `**demo.ps1**` write Jira CSV + Confluence HTML and print paths.

**Formatting:** Default Jira **CSV** uses UTF-8 BOM, all fields double-quoted, and descriptions flattened for importer stability (Hebrew-safe). `**jira-json`** and Confluence map Markdown (**bold**, ``code``) to Jira **ADF** and HTML. See `src/ama/export/` (`jira_csv.py`, `jira_sink.py`, `confluence_sink.py`).

**Standalone CSV (same file as `export-plan --format jira`):** `python tools/report_to_jira_csv.py -i report.json -o plan_jira.csv`

## Dashboard Tabs


| Tab                    | Content                                                                                                                                                                                                |
| ---------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **Executive overview** | KPIs (**% Confirmed** matches filtered-inventory merge scope), impact vs readiness scatter (importance × confidence), risk hotspots                                                                    |
| **Domains**            | Per-domain health, inventory drill-down, migration complexity scores                                                                                                                                   |
| **Planner**            | Migration waves with business + technical rationale; same output as `ama-ingest plan`; tables with manifest gaps show `is_broken` and a **Review required — manifest gaps** wave for unknown endpoints |
| **Business Glossary**  | All Hebrew ↔ English term mappings with source layer and confidence                                                                                                                                    |
| **Ask the data**       | Concept search (Hebrew and English) across the report                                                                                                                                                  |
| **Tables**             | Per-table merge breakdown + optional **pyvis** lineage neighborhood graph; manifest-unknown tables (vs `ddl_manifest_table_keys`) render as **warning** styling                                        |
| **Data quality**       | DQ suite results: boundary validation, schema version, ingestion stats                                                                                                                                 |
| **Review (HITL)**      | Approve / reject ambiguous alias mappings; writes `.hitl.json` sidecar                                                                                                                                 |
| **Migration Agent**        | Chat-first migration workflow with tool orchestration (`list_waves`, `analyze_schema`, `propose_dbt_model`, `execute_dbt_test`, `apply_fix`) and mandatory write approval gate; structured intelligence feed with per-wave progress. |


## Alias Resolution


| Step | Strategy      | Notes                                                        |
| ---- | ------------- | ------------------------------------------------------------ |
| 1    | **Glossary**  | Bilingual / business terms → high confidence                 |
| 2    | **Exact DDL** | Normalized identifier equals DDL name                        |
| 3    | **Lexical**   | Near-miss / typo tolerance                                   |
| 4    | **Semantic**  | Hash embeddings + blend; large DDL sets use vector retrieval |



| Threshold         | Default                                 | Role                                  |
| ----------------- | --------------------------------------- | ------------------------------------- |
| **Merge floor**   | `0.4` (`AMA_MERGE_CONFIDENCE_FLOOR`)    | Below → trash/review, not “confirmed” |
| **Confirmed bar** | `0.8` (`AMA_MERGE_CONFIRMED_THRESHOLD`) | Vector matches need this to confirm   |


## Environment Variables


| Variable                                                                                                                                                                                                                                                                                                                                                                                                                                                     | Purpose                                                                                                    |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------- |
| `AMA_MIGRATION_CONTEXT`                                                                                                                                                                                                                                                                                                                                                                                                                                      | Default `**schema.table`** for comms/Git anchor and single-table SQL pipeline (preferred)                  |
| `AMA_TARGET_SCHEMA` / `AMA_TARGET_TABLE`                                                                                                                                                                                                                                                                                                                                                                                                                     | Deprecated: merged into `**migration_context**` when set and context is still the default (`sales.orders`) |
| `AMA_DEFAULT_DB`                                                                                                                                                                                                                                                                                                                                                                                                                                             | Catalog when logs use `schema.table` only                                                                  |
| `AMA_DEFAULT_SQL_DIALECT`                                                                                                                                                                                                                                                                                                                                                                                                                                    | Optional SQLGlot dialect fallback when JSONL rows omit `dialect`                                           |
| `AMA_MERGE_CONFIDENCE_FLOOR`                                                                                                                                                                                                                                                                                                                                                                                                                                 | Merge floor (default `0.4`)                                                                                |
| `AMA_MERGE_CONFIRMED_THRESHOLD`                                                                                                                                                                                                                                                                                                                                                                                                                              | Vector confirm bar (default `0.8`)                                                                         |
| `AMA_DDL_COLUMNS_PATH`, `AMA_DDL_MANIFEST_PATH` (optional JSON map `schema.table` → DDL file relative to data root; per-table merge with `--discovery-merge-all`), `AMA_GLOSSARY_PATH`, `AMA_GLOSSARY_DIRTY_PATH` (optional second glossary: typos/shorthand; merged after primary; first file wins on duplicate keys), `AMA_DISCOVERY_MERGE_ALL`, `AMA_DISCOVERY_MERGE_MAX` (cap when merge-all), `AMA_SQL_LOGS_GLOB`, `AMA_COMMS_DIR`, `AMA_GIT_SQL_ROOTS` | Paths / globs                                                                                              |
| `AMA_QDRANT_PATH`                                                                                                                                                                                                                                                                                                                                                                                                                                            | Optional on-disk Qdrant                                                                                    |
| `AMA_OPENAI_API_KEY` / `OPENAI_API_KEY`                                                                                                                                                                                                                                                                                                                                                                                                                      | Optional narrative enrichment (never commit)                                                               |
| `AMA_REPORT_PATH`                                                                                                                                                                                                                                                                                                                                                                                                                                            | Default report path for dashboard                                                                          |


Load from `**.env**` in the working directory when present.

## Repository Layout


| Path                       | Role                                                                                                                                                                                                      |
| -------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `src/ama/`                 | Library and CLI                                                                                                                                                                                           |
| `src/ama/export/`          | Jira / Confluence sinks, inline Markdown helpers (`md_inline.py`)                                                                                                                                         |
| `LICENSE`                  | MIT license text                                                                                                                                                                                          |
| `demo.sh`                  | One-shot demo for Bash/Git Bash: default Kfar; `**--domain NAME**` generates a multi-domain sandbox then ingest; `**--sandbox PATH**` uses an existing tree → export-plan (Jira + Confluence)             |
| `demo.ps1`                 | One-shot demo for PowerShell/Windows: default Kfar; `**-Domain NAME**` generates a multi-domain sandbox then ingest; `**-Sandbox PATH**` uses an existing tree → export-plan (Jira + Confluence)          |
| `src/ama/planner/`         | Migration planner (waves, lineage order, rationale)                                                                                                                                                       |
| `tests/`                   | Pytest suite (100+ tests)                                                                                                                                                                                 |
| `sample_data/`             | Shared fixtures for default pipeline                                                                                                                                                                      |
| `sample_data/kfar_supply/` | **Kfar Supply demo dataset** — run `tools/generate_kfar_supply.py`                                                                                                                                        |
| `out/`                     | **Ephemeral** multi-domain sandboxes from `tools/generate_domain_data.py` (not committed)                                                                                                                 |
| `tools/`                   | Data generators: `**generate_kfar_supply.py`**, `**generate_domain_data.py**`, `**report_to_jira_csv.py**` (same CSV as default `export-plan --format jira`), `generate_sample_file.py`, chaos generators |
| `USER_GUIDE.md`            | Architecture + operator guide                                                                                                                                                                             |


## Governance & Contributing

Extend DDL paths, glossary, and discovery settings for your estate; align **merge thresholds** and **HITL** with your organization's policies.