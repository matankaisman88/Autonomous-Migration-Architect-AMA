# Autonomous Migration Architect (AMA)

**Turn legacy SQL logs into a cloud migration plan - with deterministic safety guarantees.**

AMA ingests SQL Server logs, resolves Hebrew <-> English column aliases, scores every table for migration confidence and business criticality, and generates wave-by-wave dbt migration plans with a human approval gate.
A React dashboard and REST API surface the full pipeline end-to-end.
In a real `scale_engine_chaos` run, AMA processed **340 tables in 4 minutes** and **auto bulk-approved 287**.

## Live Demo

```bash
docker compose up --build
# Open http://localhost:3000
```

Pre-loaded with the Kfar Supply dataset - a fictional Israeli wholesale distributor with legacy SQL Server schema.

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

| Layer | Technology |
|-------|-----------|
| SQL parsing | sqlglot |
| Alias resolution | Four-tier: exact -> fuzzy -> phonetic -> LLM |
| Safety scoring | Python, deterministic, zero LLM calls |
| SQL generation | OpenAI via tool-use agent loop |
| Migration planning | Autonomous planner with lineage ordering |
| API | FastAPI + WebSocket |
| Frontend | React 18 + MUI v6 + Vite |
| Tests | pytest, 179+ tests, chaos dataset with 13 edge cases |

## Key Features

- Wave-by-wave migration plan derived from co-query lineage graph
- Hebrew <-> English column alias resolution with glossary
- Broken lineage detection - tables referenced in SQL but absent from DDL
- Bulk migration with real-time WebSocket progress
- Audit trail: every automated decision logged with reason strings
- Dry run mode: full projection without any file writes
- Jira CSV + Confluence HTML export
- Multi-domain synthetic data generator for testing

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
