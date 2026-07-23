# Autonomous Migration Architect (AMA)

Internal tool used by the data engineering team to plan legacy-to-cloud database migrations.

AMA connects to a real source database (SQL Server), performs read-only extraction of table DDL and SQL query activity, resolves Hebrew <-> English column aliases, scores every table for migration confidence and business criticality, and generates wave-by-wave dbt migration plans with a human approval gate. A React dashboard and REST API surface the full pipeline end-to-end.

## Security & credentials

- **Credentials:** Store connection strings and passwords in `.env` or host environment variables (`AMA_*`). Never commit secrets to this repo. See [docs/SQLSERVER.md](docs/SQLSERVER.md) for connection-string format.
- **Network:** The API has no application-level authentication. Run it on a trusted internal network only (localhost, VPN, or private subnet). Do not expose `/api/live/start` to the public internet.
- **Source access:** Live extraction is read-only. Point AMA only at databases your team is approved to analyze.

## Quickstart — connect and extract

Run against a real SQL Server instance and produce a migration plan.

```bash
docker compose up --build
# Open http://localhost:3000 → Live connection
```

1. Open **Live connection** in the UI (or `POST /api/live/start`).
2. Enter your SQL Server connection details (host/port/user/password/database, or a full ODBC connection string).
3. Choose schema scope: **All user schemas** for the whole database, or a comma-separated list (e.g. `dbo, finance, logistics`).
4. **Test connection**, then **Start ingestion**. AMA runs a read-only extraction of table DDL (`INFORMATION_SCHEMA`) and SQL activity (Query Store / plan cache) into `live_data/<connection_name>/`.
5. Enable **Build AMA report after export** to generate the migration report, then open **Tables** to review scoring, lineage, and the wave plan.

Extraction is **read-only** — AMA never issues DDL/DML against the source database.

**Scope:** AMA produces migration **metadata and dbt artifacts** (inventory, waves, model SQL, local DuckDB validation). It does **not** bulk-replicate production table data into Snowflake/BigQuery/etc.; that cutover is your target-platform deployment and load process.

See [docs/LIVE_CONNECTION.md](docs/LIVE_CONNECTION.md) and [docs/SQLSERVER.md](docs/SQLSERVER.md).

## Local dev / testing without a live DB

If you don't have a real company database to point at, use the bundled **Kfar Supply** fixture — a synthetic dataset for local development and testing only (not part of the production flow):

```bash
# Spin up a local SQL Server loaded with the synthetic fixture
python tools/setup_dev_mssql.py
# (Re)generate the on-disk fixture artifacts under sample_data/kfar_supply/
python tools/generate_kfar_supply.py
```

Then point **Live connection** at the local `kfar_supply` database and run a normal real extraction against it. See [docs/SQLSERVER.md](docs/SQLSERVER.md) for local setup details.

**Docker default report:** the React UI pre-fills `/app/sample_data/kfar_supply/kfar_report.json` — click **Load** on the dashboard to start with the Kfar fixture without typing a path.

## Architecture

### Why deterministic scoring, not LLM scoring?

The LLM generates SQL and rationale. It does not decide which tables are safe to migrate in bulk. AMA solves trust and reproducibility risk by using a deterministic dual-axis scoring model:

- **Confidence (0-100):** Glossary match rate + type pattern coverage. AMA computes this from DDL and column mappings with zero LLM calls.
- **Criticality (0-100):** Lineage hub score + query frequency + financial keyword detection. A table with 500 queries/day and 3 downstream dependents scores 100.

**Bulk approval gate (API defaults):** Confidence >= 70 AND Criticality <= 40. Any table outside this zone routes to human review. High-criticality tables stay blocked regardless of confidence.
Additionally, discovery rows outside `ddl_manifest_table_keys` are hard-blocked (`outside_manifest_scope`) and never enter bulk automation.

### Why a Migration Contract before bulk approval?

Before AMA writes any file, it generates a Migration Contract - a short list of transformation rules for the entire batch (for example: "All DATE -> ISO 8601", "All *_id -> BIGINT"). This solves the scale-review problem by letting the architect approve policy once instead of approving every table one by one.

### Why self-healing SQL?

LLM-generated SQL fails dbt validation about 15-30% of the time on unfamiliar schemas. AMA solves that reliability gap with a bounded self-correction loop: `sqlglot` validates SQL, a Developer agent corrects it (max 3 attempts), and AMA escalates to HITL with `CRITICAL_REASON` if validation still fails.

### Data preservation guardrail

The SQL generator blocks LLM output that introduces row-level `WHERE` filters unless the user explicitly requests filtering. This guardrail solves the highest-risk failure mode in migration work: silent data loss.

```text
Logs + DDL + Glossary
        |
        v
Ingest -> Alias Resolution -> Deterministic Scoring -> Queue Assignment
                                                        | green/yellow/red
                                                        v
                                  Migration Contract -> Bulk/HITL Gate
                                                        |
                                                        v
                                   SQL Generation -> Self-Healing -> dbt Artifacts
                                                        |
                                                        v
                                        API + WebSocket + React Dashboard
```

## Stack

AMA is a Python core with a React ops UI. Deterministic layers (parsing, scoring, planning) stay LLM-free; the LLM is used only for SQL drafting, glossary translation, and agent-assisted fixes.

| Layer | Technology | Role |
| --- | --- | --- |
| **Runtime** | Python 3.11+, Docker Compose | API + dbt project in containers; local dev via `pip install -e ".[dev]"` |
| **Source connectivity** | `pyodbc` (SQL Server); optional `oracledb`, `ibm_db`, `psycopg2` | Read-only live extract — DDL from catalog views, SQL from Query Store / plan cache |
| **SQL parsing** | [sqlglot](https://github.com/tobymao/sqlglot) | Multi-dialect parse/validate (SQL Server, Oracle, DB2, DuckDB, Snowflake, BigQuery, Redshift) |
| **Log ingestion** | Chunked JSONL streaming, incremental co-occurrence | Enterprise-scale log analysis without loading full files into memory |
| **Alias resolution** | Four-tier: exact → fuzzy (`difflib`) → phonetic/hash embedding → optional LLM | Hebrew ↔ English column mapping; ambiguous matches route to **Mapping review** |
| **Semantic search** | Qdrant (+ optional `sentence-transformers`) | Vector similarity for glossary/alias candidates when embed extras installed |
| **Safety scoring** | `scale_engine/` — pure Python, zero LLM | Dual-axis **confidence** + **criticality** → green / yellow / red queues |
| **Migration planning** | `planner/` — lineage + co-query graph ordering | Wave-by-wave cutover plan with business/technical rationale |
| **Column mapping review** | `.hitl.json` sidecar + `apply_hitl_to_report` | Human approve/reject for ambiguous aliases before bulk dbt |
| **SQL generation** | OpenAI Chat Completions via tool-use agent loop | Proposed dbt model SQL + `schema.yml`; bounded token/cost tracking |
| **Self-healing SQL** | sqlglot validate + Fix Agent (max 3 attempts) | Auto-correct dbt failures; escalate to manual fix when exhausted |
| **dbt execution** | dbt + `dbt-duckdb` | Local validation against stub sources in `dbt_project/target/duckdb.db` — not production data load |
| **Model output** | `dbt_project/models/ama_generated/` | Generated `.sql` + `.schema.yml`; Checkpoint-A/B JSON artifacts |
| **API** | FastAPI + Uvicorn + WebSocket | REST for reports/migration/HITL; WS for bulk + live ingestion progress |
| **Primary UI** | React 18, MUI v6, Vite, TypeScript | Dashboard: Overview, Tables, Live connection, Mapping review, Bulk, Cockpit, Agent |
| **UI components** | MUI X Data Grid, React Flow (`@xyflow/react`), Recharts | Table inventory grid, PK/FK + co-query lineage graphs, impact charts |
| **Legacy UI** | Streamlit + Plotly | Deep Checkpoint-B / Agent UX; React is the day-to-day path |
| **CLI** | `ama-ingest`, `ama-dashboard`, `ama-api` | Ingest, DQ, plan, generate-dbt, dashboard entrypoints |
| **Synthetic data** | `ChaosFactory`, Kfar Supply fixture | 1,000+ table chaos datasets; local SQL Server dev DB via `setup_dev_mssql.py` |
| **Tests** | pytest (~320 tests) | Unit + API + chaos/scale regression; alias, parsing, dbt migration, live ingestion |


## Key Features

- Wave-by-wave migration plan derived from co-query lineage graph
- Hebrew <-> English column alias resolution with glossary
- Multi-source dialect ingestion (`sqlserver`, `oracle`, `db2`) with extensible parser abstraction
- Broken lineage detection - tables referenced in SQL but absent from DDL
- Bulk migration with real-time WebSocket progress
- Audit trail: every automated decision logged with reason strings
- Dry run mode: full projection without file writes (Streamlit dashboard and CLI evaluate paths); React **Bulk** dry run is UI-only today — it blocks execution with a notice
- Jira CSV + Confluence HTML export
- Enterprise-scale streaming log analysis (chunked processing, incremental co-occurrence, sparse similarity path)
- Multi-domain synthetic data generator for testing, including `ChaosFactory` scale generation (1,000+ tables)
- **Live connection** — read-only SQL Server extraction of real DDL + query logs via UI/API → `live_data/`

## Live Connection (SQL Server)

Connect from the React UI (**Live connection**) or `POST /api/live/start`. Extraction is **read-only** (SQL Server only): AMA reads BASE TABLE DDL from `INFORMATION_SCHEMA` and SQL text from Query Store / plan cache. No DDL/DML is deployed to the source.

- **`all_schemas: true`** — extract every user BASE TABLE in the database.
- **`schemas`** — comma-separated list (e.g. `dbo, finance, logistics`); defaults to `dbo`.

Outputs land in `live_data/<connection_name>/` (`ddl/`, `manifest.json`, `sql_logs/prod.jsonl`, optional `ama_live_report.json`). The report is built from these exported artifacts only.

> **Security:** `/api/live/start` accepts real connection strings/passwords and has **no application-level authentication** by design. Access is controlled at the **network layer only** (internal interface + firewall/VPN). Never expose this endpoint publicly. Credentials in the request body travel as plaintext JSON — front the API with TLS if it leaves the host.

**Tables tab lineage:** PK/FK schema graph with query counts on nodes and shared-query counts on edges (dashed links = SQL co-usage without DDL FK).

**Docs:** [docs/LIVE_CONNECTION.md](docs/LIVE_CONNECTION.md) · [docs/SQLSERVER.md](docs/SQLSERVER.md)

```bash
docker compose build api web && docker compose up -d
# UI → Live connection
#   • All user schemas — entire database
#   • Or schemas: dbo, finance, logistics
# → Build report → Tables tab
```

To seed Query Store / plan cache with sample application SQL when testing against the **local dev fixture**, see [tools/kfar_test_queries.sql](tools/kfar_test_queries.sql).

## Enterprise Scale + Multi-Source

Recent refactor highlights:

- `tools/generate_extreme_chaos.py` now provides a class-based `ChaosFactory` with:
  - `--scale` table generation (default `1000`)
  - `--source-dialect` selection (`sqlserver|oracle|db2`)
  - dialect-specific DDL output and multi-schema/multi-database partitioning
- `ama.log_analysis` now processes JSONL logs in **chunks** without loading all rows into memory.
- Incremental co-occurrence updates support long-running workloads and expose telemetry (`batch_id`, `chunk_id`) for observability.
- Similarity computation includes a sparse-matrix path for low-density workloads.
- DDL manifest ingestion supports source metadata (`owner`, `tablespace`, `source_dialect`) for Oracle/DB2 mapping.

### Quick generator examples

```bash
# Oracle
python tools/generate_extreme_chaos.py \
  --source-dialect oracle \
  --scale 1000 \
  --lines 200000 \
  --out chaos_data/sql_logs/extreme_oracle.jsonl \
  --ddl-out chaos_data/ddl/extreme_oracle_ddl.sql \
  --manifest-out chaos_data/ddl/extreme_oracle_manifest.json

# DB2
python tools/generate_extreme_chaos.py \
  --source-dialect db2 \
  --scale 1000 \
  --lines 200000 \
  --out chaos_data/sql_logs/extreme_db2.jsonl \
  --ddl-out chaos_data/ddl/extreme_db2_ddl.sql \
  --manifest-out chaos_data/ddl/extreme_db2_manifest.json
```

## Multi-Source + Extreme Chaos Demo

This demo proves two things:

- AMA can ingest and score high-volume logs from multiple source dialects (`sqlserver`, `oracle`, `db2`).
- AMA remains usable under noisy, high-scale workloads (large table cardinality + complex joins).

### Prerequisites

- Python 3.11+ with AMA installed (`pip install -e ".[dev]"`).
- `make` available in your shell (Git Bash/WSL/macOS/Linux).

### One-command demos (Makefile targets)

```bash
# SQL Server chaos demo (generate + ingest report)
make demo-sqlserver

# Oracle chaos demo (generate + ingest report)
make demo-oracle

# DB2 chaos demo (generate + ingest report)
make demo-db2

# Full multi-source pass (all three dialects)
make demo-multi-source

# Extreme stress pass (1M SQL log rows, SQL Server)
make demo-extreme-chaos
```

Optional scale tuning:

```bash
make demo-multi-source LINES=500000 SCALE=1500
```

Windows PowerShell (no `make`) equivalent:

```powershell
# Oracle demo
powershell -ExecutionPolicy Bypass -File .\scripts\demo-chaos.ps1 -Target oracle

# All dialects
powershell -ExecutionPolicy Bypass -File .\scripts\demo-chaos.ps1 -Target multi-source -Lines 500000 -Scale 1500

# Higher query complexity (slower, closer to extreme profile)
powershell -ExecutionPolicy Bypass -File .\scripts\demo-chaos.ps1 -Target multi-source -Lines 500000 -Scale 1500 -JoinWidth 10 -SelectColumns 24

```

`scripts/demo-chaos.ps1` options:

- `-Target`: `sqlserver | oracle | db2 | multi-source | extreme-chaos`
- `-Lines`: number of JSONL rows per dialect (default `200000`)
- `-Scale`: number of logical tables per dialect (default `1000`)
- `-JoinWidth`: joins per generated SELECT query (default `3` for faster local demos)
- `-SelectColumns`: selected synthetic columns per generated SELECT query (default `8`)
- `-Python`: Python executable to use (default `python`)

Notes:

- The script sets `AMA_SQL_PARSE_MODE=regex` for report generation to keep local demo runs responsive.
- The `extreme-chaos` target uses a heavier profile (`lines=1000000`, `scale=1000`, `join-width=10`, `select-columns=24`).

### What gets generated

- JSONL logs: `chaos_data/sql_logs/extreme_<dialect>.jsonl`
- DDL bundles: `chaos_data/ddl/extreme_<dialect>_ddl.sql`
- Dialect manifests: `chaos_data/ddl/extreme_<dialect>_manifest.json`
- AMA reports ready for loading: `sample_data/generated_chaos/<dialect>_report.json`

### Run with Docker UI/API

1. Start the app:
  ```bash
   docker compose up --build
  ```
2. Load one of the generated reports through the API:
  ```bash
   curl -X POST http://localhost:8000/report/load \
     -H "Content-Type: application/json" \
     -d "{\"path\":\"/app/sample_data/generated_chaos/oracle_report.json\"}"
  ```
   Replace `oracle_report.json` with `sqlserver_report.json` or `db2_report.json` as needed.

### Troubleshooting

- `No module named ama`: run `pip install -e ".[dev]"` in the repo root.
- `make: command not found` on Windows PowerShell: run from Git Bash/WSL or invoke the same commands directly with `python`.
- Generator is slow for very large runs: lower `LINES` and/or `SCALE` first, then scale up.
- Docker cannot load generated report path: ensure file exists under `sample_data/generated_chaos/` (mounted as `/app/sample_data/generated_chaos/` in the API container).

## Running locally (without Docker)

1. **Create a Python 3.11+ environment and install AMA**
  - `python -m venv .venv`
  - `.\.venv\Scripts\Activate.ps1` (Windows) or `source .venv/bin/activate` (macOS/Linux)
  - `pip install -e ".[dev]"`
2. **Run the API**
  - `uvicorn ama.api.main:app --host 0.0.0.0 --port 8000 --reload`
3. **Run the frontend**
  - `cd frontend`
  - `npm ci`
  - `npm run dev`
4. **Optional env config**
  - Copy `.env.example` values as needed.
  - For frontend local defaults, create `frontend/.env.local` with `VITE_AMA_API_BASE` and `VITE_DEFAULT_REPORT_PATH`.

## Project Structure

```text
src/ama/
  scale_engine/    <- deterministic scoring (no LLM)
  migration_agent/ <- agentic SQL generation + HITL gate
  dbt_migration/   <- SQL self-healing, model writing, dbt runner
  planner/         <- wave planning, lineage ordering
  api/             <- FastAPI REST + WebSocket (incl. /api/live/start)
  mcp/             <- SchemaProvider + SQL Server extract_ddl / extract_logs
  ui/              <- Streamlit dashboard (legacy, still functional)
frontend/          <- React + MUI dashboard
tests/             <- ~320 pytest tests including chaos/scale datasets
tools/             <- synthetic data generators + kfar_test_queries.sql
docs/              <- SQLSERVER.md, LIVE_CONNECTION.md
sample_data/       <- Kfar Supply dev/test fixture + scale engine chaos dataset
live_data/         <- Live connection exports (gitignored; bind-mounted in Docker)
```

## Design Decisions

AMA moved from a Streamlit-first UX to FastAPI + React because execution-heavy workflows need a richer interaction model than form reruns and polling. The current architecture keeps Streamlit available for legacy use while the React client delivers durable routing, stateful controls, and WebSocket progress for long-running actions.

AMA keeps the safety layer in deterministic Python because migration approvals must stay explainable, reproducible, and fast under repeated runs. The LLM supports SQL drafting and rationale, but it never sits in the critical safety path that decides bulk migration eligibility.

AMA treats Hebrew support as a core feature because many of the legacy SQL Server schemas we migrate internally still use mixed Hebrew and English identifiers. This bilingual reality creates real migration friction, and resolving it automatically removes a major source of manual mapping work.