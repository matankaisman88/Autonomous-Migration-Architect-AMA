# Autonomous Migration Architect (AMA)

**Turn legacy SQL logs into a cloud migration plan - with deterministic safety guarantees.**

AMA ingests SQL logs across SQL Server, Oracle, and DB2, resolves Hebrew <-> English column aliases, scores every table for migration confidence and business criticality, and generates wave-by-wave dbt migration plans with a human approval gate.
A React dashboard and REST API surface the full pipeline end-to-end.
The default Docker dataset (Kfar Supply) is SQL Server-focused; use `tools/generate_extreme_chaos.py` or the `Makefile` demo targets below for Oracle/DB2/multi-source chaos inputs.
In a real `scale_engine_chaos` run, AMA processed **340 tables in 4 minutes** and **auto bulk-approved 287**.

## Live Demo

```bash
docker compose up --build
# Open http://localhost:3000
```

Pre-loaded with the Kfar Supply dataset - a fictional Israeli wholesale distributor with legacy SQL Server schema.

Want Oracle/DB2 variants for the same flow? Generate them with `tools/generate_extreme_chaos.py --source-dialect <dialect>` or run `make demo-multi-source`.

## Architecture

### Why deterministic scoring, not LLM scoring?

The LLM generates SQL and rationale. It does not decide which tables are safe to migrate in bulk. AMA solves trust and reproducibility risk by using a deterministic dual-axis scoring model:

- **Confidence (0-100):** Glossary match rate + type pattern coverage. AMA computes this from DDL and column mappings with zero LLM calls.
- **Criticality (0-100):** Lineage hub score + query frequency + financial keyword detection. A table with 500 queries/day and 3 downstream dependents scores 100.

**Bulk approval gate:** Confidence >= 90 AND Criticality < 40. Any table outside this zone routes to human review. High-criticality tables stay blocked regardless of confidence.

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


| Layer              | Technology                                           |
| ------------------ | ---------------------------------------------------- |
| SQL parsing        | sqlglot                                              |
| Alias resolution   | Four-tier: exact -> fuzzy -> phonetic -> LLM         |
| Safety scoring     | Python, deterministic, zero LLM calls                |
| SQL generation     | OpenAI via tool-use agent loop                       |
| Migration planning | Autonomous planner with lineage ordering             |
| API                | FastAPI + WebSocket                                  |
| Frontend           | React 18 + MUI v6 + Vite                             |
| Tests              | pytest, 179+ tests, chaos dataset with 13 edge cases |


## Key Features

- Wave-by-wave migration plan derived from co-query lineage graph
- Hebrew <-> English column alias resolution with glossary
- Multi-source dialect ingestion (`sqlserver`, `oracle`, `db2`) with extensible parser abstraction
- Broken lineage detection - tables referenced in SQL but absent from DDL
- Bulk migration with real-time WebSocket progress
- Audit trail: every automated decision logged with reason strings
- Dry run mode: full projection without any file writes
- Jira CSV + Confluence HTML export
- Enterprise-scale streaming log analysis (chunked processing, incremental co-occurrence, sparse similarity path)
- Multi-domain synthetic data generator for testing, including `ChaosFactory` scale generation (1,000+ tables)

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
  api/             <- FastAPI REST + WebSocket
  ui/              <- Streamlit dashboard (legacy, still functional)
frontend/          <- React + MUI dashboard
tests/             <- 179+ tests including chaos dataset
tools/             <- synthetic data generators
sample_data/       <- Kfar Supply demo + scale engine chaos dataset
```

## Design Decisions

AMA moved from a Streamlit-first UX to FastAPI + React because execution-heavy workflows need a richer interaction model than form reruns and polling. The current architecture keeps Streamlit available for legacy use while the React client delivers durable routing, stateful controls, and WebSocket progress for long-running actions.

AMA keeps the safety layer in deterministic Python because migration approvals must stay explainable, reproducible, and fast under repeated runs. The LLM supports SQL drafting and rationale, but it never sits in the critical safety path that decides bulk migration eligibility.

AMA treats Hebrew support as a core feature because many Israeli enterprise systems still run legacy SQL Server schemas with mixed Hebrew and English identifiers. This bilingual reality creates real migration friction, and resolving it automatically gives AMA practical differentiation beyond toy demos.