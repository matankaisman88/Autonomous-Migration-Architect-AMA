# Autonomous Migration Architect (AMA)

**Vision:** AMA turns legacy **SQL logs**, **Git-resident SQL**, and **communications** into **evidence-backed migration intelligence** — inventory, column-to-DDL alignment, lineage-aware risk, human review (HITL), and repeatable **JSON / Excel / dashboard** outputs. It is the core engine for **planning waves**, **data-quality gates**, and **executive storytelling** in large modernization programs.

| | |
|---|---|
| **Purpose** | Legacy-to-cloud migration intelligence from **SQL logs**, **Git SQL**, and **comms** |
| **Outputs** | JSON / Markdown / **Excel** reports, **Streamlit** dashboard, **DQ** + **plan** CLI |
| **Python** | 3.11+ |

---

## Architecture (library layout)

| Package / module | Role |
|------------------|------|
| **`ama.parsing`** | SQLGlot-backed parse + regex fallback |
| **`ama.sql_pipeline`** | Streaming JSONL ingestion, telemetry, optional lineage |
| **`ama.log_analysis`** | **Log Analysis Engine** — streaming scan + parse telemetry facade (`LogAnalysisEngine`) |
| **`ama.discovery`** | Full-log scan, domain enrichment, **`migration_state`** (domain order, co-query clusters) |
| **`ama.planner`** | **Autonomous Planner** — system-wide migration **waves** from discovery inventory + lineage |
| **`ama.data_quality`** | **DQ** — report boundary + contract checks (`run_dq_suite`) |
| **`ama.security`** | Path redaction, safe path helpers (no secrets in logs) |
| **`ama.schemas`** | Pydantic report contracts |
| **`ama.ui`** | Streamlit dashboard |

Credentials and paths: use **`AMA_*` environment variables** and **`.env`** (see below). Do not commit secrets.

---

## What AMA does

| Layer | Role |
|-------|------|
| **Ingestion** | Sanitize text (NFC, control chars), parse with **SQLGlot** (dialect registry in `ama.parsing`), **regex fallback** when parsing fails |
| **Alias resolution** | Map log columns → target **DDL** via glossary → exact match → lexical + hash embeddings; **merge floor** / **confirmed threshold** protect quality |
| **Reporting** | Importance, discovery inventory, **Migration** sheet, **HITL** review queue |

---

## Technical stack & dependencies

| Area | Choice |
|------|--------|
| **Language** | Python 3.11+ |
| **SQL parsing** | [sqlglot](https://github.com/tobymao/sqlglot) |
| **Validation** | [pydantic](https://docs.pydantic.dev/) v2, [pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/) |
| **Tabular / Excel** | pandas, openpyxl |
| **Embeddings / vector store** | numpy; optional Qdrant (`qdrant-client`) |
| **Dashboard** | Streamlit, Plotly |
| **Demo CLI** | [rich](https://github.com/Textualize/rich) (terminal UI for `demo_runner.py`) |
| **Optional viz** | pyvis (interactive lineage) — `pip install -e ".[viz]"` |

Core dependencies are declared in **`pyproject.toml`**.

---

## Installation & setup

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

---

## Commands (CLI)

| Command | Action |
|---------|--------|
| `ama-ingest run --help` | Full ingestion (SQL logs, comms, Git, importance) |
| `ama-ingest run --format json -o report.json` | JSON report |
| `ama-ingest run --format excel -o report.xlsx` | Excel workbook |
| `ama-ingest run --discovery-mode` | **System-wide** discovery: full inventory + lineage (planner, risk hotspots, domain processing order) |
| `ama-ingest run --discovery-mode --discovery-merge-all` | DDL merge on **every** discovered table using `ddl_manifest.json` + default `orders_columns.json` fallback |
| `ama-ingest run --migration-context SCHEMA.TABLE` | Override **`AMA_MIGRATION_CONTEXT`** (comms/Git anchor and single-table scope); replaces legacy `--target-schema` / `--target-table` for most uses |
| `ama-ingest dq --report report.json` | **Data quality** checks on a report JSON |
| `ama-ingest plan --report report.json` | **Migration plan** JSON (waves from inventory) |
| `ama-ingest log-scan PATH [PATH...]` | Stream-scan SQL **`.jsonl`** logs → parse telemetry JSON |
| `ama-ingest apply-hitl --report report.json` | Apply `.hitl.json` → merged JSON/Excel |
| `ama-ingest run --benchmark` | Performance benchmark → `benchmark_results.json` |
| `ama-ingest run --stress` | Extreme log stress → `stress_report.json` |
| `ama-dashboard --report-path report.json` | Streamlit UI |
| `python demo_runner.py` | **Stakeholder demo** — runs the same ingest as `ama-ingest run --discovery-mode --discovery-merge-all`, writes `demo_report.json`, Rich terminal UI, optional Streamlit + browser |

JSON reports include **`schema_version`** (e.g. **`1.2`**), **`ingestion_stats`**, **`migration_context`** (qualified `schema.table` for comms/Git anchor — not the merge limit when discovery is on), **`system_scope`** (e.g. `system_wide` vs `single_table`), **`merge_scope`** (how DDL merge was scoped: single-table logs, top‑N, or all discovered; uses **`comms_git_reference`**), and with **`--discovery-mode`** additive **`lineage`**, **`discovery.migration_state`** (domains detected, lineage-aware **domain processing order**, co-query clusters), plus executive **`risk_hotspots`** when the graph has edges. Legacy field **`target_table`** may still appear in older exports; tools accept **`migration_context`** or **`target_table`**.

---

## Sanitization (summary)

| Input issue | Handling |
|-------------|----------|
| Null bytes / C0 controls | Stripped (safe whitespace kept) |
| Unicode | **NFC** normalization |
| Identifiers | Quotes/brackets stripped; ASCII **casefold**; non-ASCII preserved |
| RTL (Hebrew, etc.) | Display helpers for LTR consoles; Excel/dashboard are canonical |

---

## Alias resolution order

| Step | Strategy | Notes |
|------|------------|--------|
| 1 | **Glossary** | Bilingual / business terms → high confidence |
| 2 | **Exact DDL** | Normalized identifier equals DDL name |
| 3 | **Lexical** | Near-miss / typo tolerance |
| 4 | **Semantic** | Hash embeddings + blend; large DDL sets use vector retrieval |

| Threshold | Default | Role |
|-----------|---------|------|
| **Merge floor** | `0.4` (`AMA_MERGE_CONFIDENCE_FLOOR`) | Below → trash/review, not “confirmed” |
| **Confirmed bar** | `0.8` (`AMA_MERGE_CONFIRMED_THRESHOLD`) | Vector matches need this to confirm |

---

## Dashboard tabs

| Tab | Content |
|-----|---------|
| **Executive overview** | System overview metrics, KPIs, impact vs readiness scatter, domain importance, risk hotspots |
| **Domains** | Per-domain health and inventory |
| **Business Glossary** | Grouped legacy → DDL stories; **full `sample_data/glossary/` inventory** in JSON report (`glossary_source`) + dashboard expander |
| **Ask the data** | Concept search (Hebrew/English) |
| **Tables** | Per-table merge breakdown; optional **pyvis** lineage neighborhood |
| **Data quality** | Same checks as **`ama-ingest dq`** (boundary, schema, discovery inventory) |
| **Planner** | System-wide migration waves by domain (lineage-aware order) — same as **`ama-ingest plan`** |
| **Review (HITL)** | Approve/reject; sidecar `<report>.hitl.json` |

**HITL in the dashboard:** With a **file path** (not upload), the UI merges `.hitl.json` into the loaded report on each run so Executive / Glossary / metrics reflect approvals immediately. Use **Reload from Disk** after regenerating the JSON. To produce a merged file for sharing:

```bash
ama-ingest apply-hitl --report report.json --format excel -o report.with_hitl.xlsx
```

---

## Environment variables (`AMA_*`)

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

---

## Repository layout

| Path | Role |
|------|------|
| `src/ama/` | Library and CLI (`parsing/`, `schemas/`, `log_analysis/`, `planner/`, `data_quality/`, `security/`, `lineage.py`, …) |
| `tests/` | Pytest suite |
| `sample_data/` | Small fixtures for CI and demos |
| `tools/` | Generators (`generate_*`, stress helpers) |
| `demo_runner.py` | Stakeholder demo: full discovery + merge-all ingest, plan snapshot, optional dashboard |
| `USER_GUIDE.md` | Reader + architecture overview |
| `CONTRIBUTING.md` | Dev quick reference |

**`chaos_data/`** is **gitignored** (generated). Create it with:

```bash
python tools/generate_full_db_chaos.py
python tools/generate_extreme_chaos.py --lines 1000000 --out chaos_data/sql_logs/extreme_1m.jsonl
```

### Demo runner (`demo_runner.py`)

From the repo root (after `pip install -e .`):

```bash
python demo_runner.py
python demo_runner.py --no-dashboard
python demo_runner.py --skip-vectors
python demo_runner.py --all-project-sql-logs
```

This invokes **`ama.cli.cmd_run`** with **`--discovery-mode`** and **`--discovery-merge-all`** (same feature set as `ama-ingest run` with those flags), writes **`demo_report.json`** by default, prints a **Rich** summary table, then optionally starts **Streamlit** and opens `http://localhost:8501`. Use **`--report-out`** to change the JSON path (use the same path with **`ama-dashboard --report-path`** if you launch the UI yourself).

---

## Git release (example)

| Step | Command |
|------|---------|
| Commit | `git add -A && git commit -m "..."` |
| Tag | `git tag -a v1.0.0 -m "AMA v1.0.0"` |
| Push | `git push origin main && git push origin v1.0.0` |

---

## Governance

Extend DDL paths, glossary, and discovery settings for your estate; align **merge thresholds** and **HITL** with your organization’s policies.
