# Autonomous Migration Architect (AMA)

**Turn legacy SQL logs into an evidence-backed cloud migration plan — automatically.**

AMA reads your SQL logs, resolves column aliases (including Hebrew ↔ English), discovers
your full table inventory, and produces wave-by-wave migration plans with business rationale.
No manual inventory. No spreadsheets. Repeatable from day one.

| | |
|---|---|
| **Input** | SQL log JSONL files, DDL JSON, Slack/comms export, Git SQL |
| **Output** | JSON report · Excel workbook · Streamlit dashboard · Jira bulk-create · Confluence page |
| **Python** | 3.11+ · [MIT License](LICENSE) |

## Features (high level)

- **Discovery + lineage** — Multi-schema inventory, domain clustering, and an undirected co-query lineage graph (bare schema-name tokens from the parser are filtered so edges only connect real `schema.table` keys).
- **Alias merge scope** — Default single-table merge for the migration anchor, or **`--discovery-merge-all`** to run the four-tier resolver against every discovered table in DDL scope (recommended for the Kfar demo so review-band candidates appear across schemas).
- **Exports** — **`ama-ingest export-plan`** writes Jira bulk-create JSON (ADF descriptions) or Confluence wiki storage HTML. Inline Markdown in rationales (**bold**, `` `code` ``) is converted to Jira ADF `strong` / `code` marks and HTML `<strong>` / `<code>`.
- **Glossary tooling** — **`ama-ingest generate-glossary`** mines Hebrew/RTL ↔ English co-occurrences from SQL logs into a candidate glossary JSON (optional LLM assist).
- **One-command demo** — Repository root **`demo.sh`** (Bash/Git Bash): regenerate Kfar data → ingest with discovery + merge-all → Jira + Confluence exports → prints output paths.
- **Multi-domain fixtures** — **`tools/generate_domain_data.py`** builds a full AMA sandbox (DDL, JSONL logs, glossary, comms, Git SQL, README) for **`finance`**, **`hr`**, **`logistics`**, **`retail`**, or **`healthcare`**, under **`out/sandbox_{domain}_YYYYMMDD_HHMMSS/`**. Run **`bash demo.sh --sandbox …`** with the printed path to ingest that tree instead of Kfar (no new dependencies).

## Demo (30 seconds)

> **Dataset:** Kfar Supply Ltd. — fictional Israeli wholesale distributor,
> 8-table SQL Server legacy database, migrating to Azure Synapse.

**Option A — full toolchain in one script (Bash):**

```bash
bash demo.sh
```

**Option B — same steps manually (PowerShell-friendly one-liners):**

```bash
python tools/generate_kfar_supply.py
ama-ingest run --sql-logs "sample_data/kfar_supply/sql_logs/*.jsonl" --ddl-manifest sample_data/kfar_supply/ddl/kfar_manifest.json --glossary sample_data/kfar_supply/glossary/kfar_glossary.json --glossary-dirty sample_data/kfar_supply/glossary/kfar_glossary_dirty.json --comms-dir sample_data/kfar_supply/comms --git-sql-roots sample_data/kfar_supply/git_sql --target-schema dbo --target-table orders --discovery-mode --discovery-merge-all --format json -o kfar_report.json
ama-ingest export-plan --report kfar_report.json --format jira --out kfar_export_jira.json
ama-ingest export-plan --report kfar_report.json --format confluence --out kfar_export_confluence.html
ama-dashboard --report-path kfar_report.json
```

See **`sample_data/kfar_supply/README.md`** for a step-by-step quickstart (optional `generate-glossary`, explicit log path, and `--ddl-columns`).

### Multi-domain sandbox (optional)

Generate an isolated fixture set for another vertical, then point **`demo.sh`** at it:

```bash
python tools/generate_domain_data.py --domain hr --lines 10000 --seed 42
bash demo.sh --sandbox out/sandbox_hr_YYYYMMDD_HHMMSS   # exact path printed by the generator
```

Each run writes a timestamped directory under **`out/`** (gitignored) with **`ddl/manifest.json`**, **`sql_logs/{domain}_prod.jsonl`**, **`glossary/{domain}_glossary*.json`**, **`comms/`**, and **`git_sql/`**. The printed **`--sandbox`** path is the same tree **`demo.sh`** expects.

**What you'll see in the dashboard:**

- Executive overview: 6 domains, risk hotspots, impact vs readiness scatter
- Planner tab: migration waves (Finance: invoices → payments ordered by lineage)
- Business Glossary: Hebrew ↔ English column mappings with confidence scores
- HITL Review: typically **3–4** ambiguous mappings across schemas when using **`--discovery-merge-all`** (compound DDL names without underscores, review band ~0.4–0.8)
- Tables tab: interactive lineage graph (dbo.orders is the hub)

## How It Works

**Step 1 — Ingest.** AMA streams SQL JSONL logs through a SQLGlot parser (with regex fallback), extracting table references and column usage patterns. Every record is sanitized (NFC normalization, null-byte stripping, RTL display handling for Hebrew identifiers).

**Step 2 — Resolve.** Each column seen in logs is matched to your target DDL through a four-tier alias pipeline: glossary lookup → exact normalized match → lexical near-miss → hash-embedding vector similarity. Confidence scores drive three buckets: merged (≥0.8), review queue (0.4–0.8), and discarded (<0.4).

**Step 3 — Discover.** In discovery mode, AMA inventories every `schema.table` seen across all logs, scores each by query volume (priority score = % of max), classifies business domains, detects technical debt, builds a co-query lineage graph, and generates executive risk hotspots.

**Step 4 — Plan.** The Autonomous Planner groups tables by business domain, orders them using Kahn's topological sort over the lineage graph (dependency-safe wave ordering), and annotates each wave with business rationale and technical notes drawn from the report data.

## Architecture

| Package / module | Role |
|---|---|
| `ama.parsing` | SQLGlot-backed SQL parse + regex fallback |
| `ama.sql_pipeline` | Streaming JSONL ingestion, telemetry, lineage accumulation |
| `ama.lineage` | Undirected co-query graph (`LineageGraph`); edges use real `schema.table` keys only |
| `ama.alias_resolver` | 4-tier column alias resolution (glossary → exact → lexical → vector) |
| `ama.discovery` | Multi-schema inventory, priority scoring, domain classification |
| `ama.business_logic` | Executive narrative, domain clustering, impact/readiness scatter |
| `ama.planner` | Migration waves: Kahn topo-sort over lineage graph + business rationales |
| `ama.export` | Jira bulk-create JSON and Confluence HTML export sinks; Markdown ** / `` ` `` → ADF / HTML |
| `ama.glossary` | Co-occurrence mining + optional LLM translation for candidate glossaries |
| `ama.data_quality` | DQ suite: boundary validation, schema version, ingestion stats checks |
| `ama.log_analysis` | Streaming log scan facade (no full ingest — telemetry only) |
| `ama.security` | Path redaction, secret masking, path-traversal guard |
| `ama.schemas` | Pydantic report contracts (`AmaReportBoundarySchema`, schema version) |
| `ama.ui` | Streamlit dashboard (8 tabs) |

## Installation

```bash
git clone <repository-url> && cd Autonomous-Migration-Architect-AMA
python -m venv .venv
# Windows: .venv\Scripts\activate
# Unix: source .venv/bin/activate
pip install -e .
# Optional: heavier embeddings
pip install -e ".[embed]"
# Optional: interactive lineage graph in the dashboard (Tables tab)
pip install -e ".[viz]"
```

Create a **`.env`** file in the working directory (optional) with `AMA_*` variables — loaded automatically by `IngestionSettings`.

## CLI Reference

| Command | What it does |
|---|---|
| `ama-ingest run` | Full ingestion: SQL logs + comms + Git + importance scoring |
| `ama-ingest run --discovery-mode` | All of `run` + full multi-schema discovery inventory + lineage graph |
| `ama-ingest run --format excel -o report.xlsx` | Excel workbook output |
| `ama-ingest dq --report report.json` | Data quality checks (boundary, schema version, ingestion stats) |
| `ama-ingest plan --report report.json` | Migration plan JSON from discovery inventory |
| `ama-ingest export-plan --report report.json --format jira` | Jira bulk-create JSON (epics + stories per wave) |
| `ama-ingest export-plan --report report.json --format confluence` | Confluence wiki storage HTML |
| `ama-ingest generate-glossary …` | Mine SQL logs + DDL → candidate glossary JSON (see `--help`) |
| `ama-ingest log-scan PATH [PATH...]` | Streaming log scan → parse telemetry JSON (no full report) |
| `ama-ingest apply-hitl --report report.json` | Apply HITL sidecar decisions → merged report |
| `ama-ingest run --benchmark` | Performance benchmark → `benchmark_results.json` |
| `ama-dashboard --report-path report.json` | Launch Streamlit dashboard |

## Dashboard Tabs

| Tab | Content |
|---|---|
| **Executive overview** | KPIs, impact vs readiness scatter (importance × confidence), risk hotspots |
| **Domains** | Per-domain health, inventory drill-down, migration complexity scores |
| **Planner** | Migration waves with business + technical rationale; same output as `ama-ingest plan` |
| **Business Glossary** | All Hebrew ↔ English term mappings with source layer and confidence |
| **Ask the data** | Concept search (Hebrew and English) across the report |
| **Tables** | Per-table merge breakdown + optional pyvis lineage neighborhood graph |
| **Data quality** | DQ suite results: boundary validation, schema version, ingestion stats |
| **Review (HITL)** | Approve / reject ambiguous alias mappings; writes `.hitl.json` sidecar |

## Alias Resolution

| Step | Strategy | Notes |
|------|------------|--------|
| 1 | **Glossary** | Bilingual / business terms → high confidence |
| 2 | **Exact DDL** | Normalized identifier equals DDL name |
| 3 | **Lexical** | Near-miss / typo tolerance |
| 4 | **Semantic** | Hash embeddings + blend; large DDL sets use vector retrieval |

| Threshold | Default | Role |
|-----------|---------|--------|
| **Merge floor** | `0.4` (`AMA_MERGE_CONFIDENCE_FLOOR`) | Below → trash/review, not “confirmed” |
| **Confirmed bar** | `0.8` (`AMA_MERGE_CONFIRMED_THRESHOLD`) | Vector matches need this to confirm |

## Environment Variables

| Variable | Purpose |
|----------|---------|
| `AMA_MIGRATION_CONTEXT` | Default **`schema.table`** for comms/Git anchor and single-table SQL pipeline (preferred) |
| `AMA_TARGET_SCHEMA` / `AMA_TARGET_TABLE` | Deprecated: merged into **`migration_context`** when set and context is still the default (`sales.orders`) |
| `AMA_DEFAULT_DB` | Catalog when logs use `schema.table` only |
| `AMA_DEFAULT_SQL_DIALECT` | Optional SQLGlot dialect fallback when JSONL rows omit `dialect` |
| `AMA_MERGE_CONFIDENCE_FLOOR` | Merge floor (default `0.4`) |
| `AMA_MERGE_CONFIRMED_THRESHOLD` | Vector confirm bar (default `0.8`) |
| `AMA_DDL_COLUMNS_PATH`, `AMA_DDL_MANIFEST_PATH` (optional JSON map `schema.table` → DDL file relative to data root; per-table merge with `--discovery-merge-all`), `AMA_GLOSSARY_PATH`, `AMA_GLOSSARY_DIRTY_PATH` (optional second glossary: typos/shorthand; merged after primary; first file wins on duplicate keys), `AMA_DISCOVERY_MERGE_ALL`, `AMA_DISCOVERY_MERGE_MAX` (cap when merge-all), `AMA_SQL_LOGS_GLOB`, `AMA_COMMS_DIR`, `AMA_GIT_SQL_ROOTS` | Paths / globs |
| `AMA_QDRANT_PATH` | Optional on-disk Qdrant |
| `AMA_OPENAI_API_KEY` / `OPENAI_API_KEY` | Optional narrative enrichment (never commit) |
| `AMA_REPORT_PATH` | Default report path for dashboard |

Load from **`.env`** in the working directory when present.

## Repository Layout

| Path | Role |
|---|---|
| `src/ama/` | Library and CLI |
| `src/ama/export/` | Jira / Confluence sinks, inline Markdown helpers (`md_inline.py`) |
| `LICENSE` | MIT license text |
| `demo.sh` | One-shot demo: default Kfar path, or **`--sandbox PATH`** for a **`generate_domain_data.py`** tree → ingest → export-plan (Jira + Confluence) |
| `src/ama/planner/` | Migration planner (waves, lineage order, rationale) |
| `tests/` | Pytest suite (100+ tests) |
| `sample_data/` | Shared fixtures for default pipeline |
| `sample_data/kfar_supply/` | **Kfar Supply demo dataset** — run `tools/generate_kfar_supply.py` |
| `out/` | **Ephemeral** multi-domain sandboxes from `tools/generate_domain_data.py` (not committed) |
| `tools/` | Data generators: **`generate_kfar_supply.py`**, **`generate_domain_data.py`**, `generate_sample_file.py`, chaos generators |
| `USER_GUIDE.md` | Architecture + operator guide |

## Governance & Contributing

Extend DDL paths, glossary, and discovery settings for your estate; align **merge thresholds** and **HITL** with your organization's policies.
