# AMA dbt Migration Guide

This guide documents the `ama-ingest generate-dbt` workflow for transforming AMA report metadata into executable dbt models with HITL gating and controlled execution.

## Workflow Overview

At a high level, `generate-dbt` performs a gated multi-agent flow:

1. Validate runtime `TARGET_DIALECT`.
2. Read AMA report inventory/lineage and optional glossary.
3. Run role-specialized generation:
   - **Architect**: plans schema/mapping decisions (including Hebrew mapping context).
   - **Developer**: drafts dbt SQL/YAML.
   - **QA Lead**: validates generated SQL with `sqlglot` before exposing it for approval.
4. Produce **Checkpoint A** payload for mandatory human review.
5. If QA rejects syntax/logic, trigger a bounded **self-healing loop**:
   - Developer self-correction attempts: **max 3**.
   - On exhaustion, emit `CRITICAL_REASON` and require HITL intervention.
6. After explicit approval, write model files and optionally execute `dbt run` + `dbt test`.
7. On execution failure, enter execution fix loop (bounded retries); route exhausted failures to DLQ.

## Report sources

AMA reports consumed by `generate-dbt`, bulk migration, and the React UI can come from:

| Source | Typical path | Notes |
| --- | --- | --- |
| Live connection export | `live_data/<connection_name>/ama_live_report.json` | **Primary path.** Built by UI/API after read-only extraction from a real SQL Server; see [docs/LIVE_CONNECTION.md](docs/LIVE_CONNECTION.md) |
| Local dev fixture (Kfar Supply) | `sample_data/kfar_supply/kfar_report.json` | Dev/test only — synthetic dataset with Hebrew glossary, comms, git SQL |
| Chaos / scale test data | `sample_data/generated_chaos/*_report.json` | Dev/test only — multi-dialect stress datasets |

Load via UI auto-load (Live page) or API:

```bash
curl -X POST http://localhost:8000/report/load \
  -H "Content-Type: application/json" \
  -d "{\"path\":\"/app/live_data/demo/ama_live_report.json\"}"
```

Use the host path under your repo when calling from outside Docker (e.g. `C:/.../live_data/demo/ama_live_report.json` on Windows).

**Real extraction reports** do not include bundled `sample_data/kfar_supply` glossary rows — only log + DDL discovery mappings.

## CLI Reference

Command:

```bash
# Bash / Git Bash
ama-ingest generate-dbt \
  --report REPORT.json \
  --target-dialect duckdb \
  --output-dir models/ama_generated
```

```powershell
# PowerShell (Windows)
ama-ingest generate-dbt `
  --report REPORT.json `
  --target-dialect duckdb `
  --output-dir models/ama_generated
```

| Flag | Required | Type | Description |
| --- | --- | --- | --- |
| `--report` | Yes | `str` | Path to AMA report JSON. |
| `--target-dialect` | Yes | `str` | Runtime target dialect. Supported: `duckdb`, `snowflake`, `bigquery`, `redshift`. |
| `--glossary` | No | `str` | Path to AMA glossary JSON for Hebrew-English mapping. |
| `--output-dir` | No | `str` | Output dbt models directory. Equivalent to `--models-dir` in current CLI wiring. |
| `--dbt-project-dir` | No | `str` | dbt project root. Default: parent of `--models-dir`. |
| `--approve-checkpoint-a` | No | flag | Approves Checkpoint A and allows artifact write/execution. |
| `--run-dbt` | No | flag | Runs `dbt run` and `dbt test` after approval. |

## Source Dialect Setup (Oracle / DB2)

AMA now supports non-SQLServer source metadata/log ingestion for Oracle and DB2.

### 1) Generate large synthetic source data for Oracle/DB2

```bash
# Bash / Git Bash
python tools/generate_extreme_chaos.py \
  --source-dialect oracle \
  --scale 1000 \
  --lines 1000000
```

```powershell
# PowerShell (Windows)
python tools/generate_extreme_chaos.py `
  --source-dialect db2 `
  --scale 1000 `
  --lines 1000000
```

This writes:
- dialect-aware DDL (`--ddl-out`)
- partitioned multi-schema table manifest (`--manifest-out`)
- streaming SQL JSONL log output (`--out`)

### 2) Ingest Oracle/DB2 logs with chunked analysis

```bash
# Bash / Git Bash
ama-ingest log-scan \
  --sql-logs chaos_data/sql_logs/extreme_1m.jsonl \
  --all-envs \
  --progress
```

`log-scan` now processes in chunks and emits telemetry with `batch_id` and `chunk_id` for long runs.

### 3) DDL manifest metadata for Oracle/DB2

Use rich manifest entries to preserve source details:

```json
{
  "finance.orders": {
    "path": "sample_data/ddl/orders_columns.json",
    "source_dialect": "oracle",
    "owner": "FINANCE_APP",
    "tablespace": "TS_FIN_01",
    "schema": "FINANCE"
  }
}
```

`owner` and `tablespace` are extracted by the parser for Oracle/DB2 `CREATE TABLE` statements and are available to downstream merge/report logic.

Example (generate only, hold for review):

```bash
# Bash / Git Bash
ama-ingest generate-dbt \
  --report sample_data/kfar_supply/kfar_report.json \
  --target-dialect duckdb \
  --glossary sample_data/kfar_supply/glossary/kfar_glossary.json \
  --output-dir models/ama_generated
```

```powershell
# PowerShell (Windows)
ama-ingest generate-dbt `
  --report sample_data/kfar_supply/kfar_report.json `
  --target-dialect duckdb `
  --glossary sample_data/kfar_supply/glossary/kfar_glossary.json `
  --output-dir models/ama_generated
```

Example (approve and execute):

```bash
# Bash / Git Bash
ama-ingest generate-dbt \
  --report sample_data/kfar_supply/kfar_report.json \
  --target-dialect duckdb \
  --approve-checkpoint-a \
  --run-dbt \
  --dbt-project-dir .
```

```powershell
# PowerShell (Windows)
ama-ingest generate-dbt `
  --report sample_data/kfar_supply/kfar_report.json `
  --target-dialect duckdb `
  --approve-checkpoint-a `
  --run-dbt `
  --dbt-project-dir .
```

## Checkpoint A

Checkpoint A is a mandatory intermediate state that exposes generation artifacts before execution.

### Why it exists

- Prevents execution before human validation.
- Surfaces mapping fallbacks and unresolved lineage early.
- Creates a deterministic handoff for corrections/rework.

### JSON example

```json
{
  "wave_summary": "Wave 2: Finance tables prioritized by query volume and lineage dependencies.",
  "generated_models": [
    {
      "table_key": "dbo.orders",
      "model_name": "dbo_orders",
      "review_required": false,
      "is_stub": false,
      "sql": "{{ config(materialized='view') }}\n\nSELECT ...",
      "schema_yml": "version: 2\nmodels:\n  - name: dbo_orders\n    columns: []\n"
    },
    {
      "table_key": "ghost_system.payments_archive",
      "model_name": "ghost_system_payments_archive",
      "review_required": true,
      "is_stub": true,
      "sql": "-- WARNING: UNRESOLVED BROKEN LINEAGE\n...",
      "schema_yml": "version: 2\nmodels:\n  - name: ghost_system_payments_archive\n    columns: []\n"
    }
  ],
  "mapping_rows": [
    {
      "hebrew_name": "סכום_כולל",
      "english_alias": "total_amount",
      "source": "Glossary",
      "warning_flags": []
    },
    {
      "hebrew_name": "תאור",
      "english_alias": "taor",
      "source": "Transliteration",
      "warning_flags": ["[TRANSLITERATION_WARNING]"]
    }
  ],
  "review_required_tables": [
    "ghost_system.payments_archive",
    "dbo.customers"
  ]
}
```

### YAML view example

```yaml
wave_summary: "Wave 2: Finance tables prioritized by query volume and lineage dependencies."
generated_models:
  - table_key: "dbo.orders"
    model_name: "dbo_orders"
    review_required: false
    is_stub: false
  - table_key: "ghost_system.payments_archive"
    model_name: "ghost_system_payments_archive"
    review_required: true
    is_stub: true
mapping_rows:
  - hebrew_name: "סכום_כולל"
    english_alias: "total_amount"
    source: "Glossary"
    warning_flags: []
  - hebrew_name: "תאור"
    english_alias: "taor"
    source: "Transliteration"
    warning_flags:
      - "[TRANSLITERATION_WARNING]"
review_required_tables:
  - "ghost_system.payments_archive"
  - "dbo.customers"
```

## DLQ (Dead Letter Queue) Specification

Models rejected after max fix attempts are routed to DLQ.

### Required fields

- `original_payload`
- `error_reason`
- `error_stage`
- `timestamp`
- `run_id`

### JSONL record format

```json
{
  "original_payload": {
    "model_name": "dbo_orders",
    "sql": "select ...",
    "attempt_count": 3
  },
  "error_reason": "column does not exist: customer_segment_id",
  "error_stage": "dbt_run",
  "timestamp": "2026-03-25T14:32:01.123456+00:00",
  "run_id": "c8d233ec-79e1-4c3c-a8df-5ef8e3d862d6"
}
```

## Localization and Mapping

Mapping uses glossary first, transliteration fallback second.

| Hebrew Name | English Alias | Source | Notes |
| --- | --- | --- | --- |
| `סכום_כולל` | `total_amount` | Glossary | Stable business term across wave. |
| `תאריך_הזמנה` | `order_date` | Glossary | Used for date dimension filters. |
| `תאור` | `taor` | Transliteration | Flagged with `[TRANSLITERATION_WARNING]` and requires review. |

Business logic example:

```sql
select
  "סכום_כולל" as total_amount,
  cast("תאריך_הזמנה" as date) as order_date
from "dbo.orders"
```

## Fix Loop Logic

When `--run-dbt` is enabled:

1. Execute `dbt run` then `dbt test`.
2. On failure, parse error signatures (`column does not exist`, `type mismatch`, `syntax error`, fallback unknown).
3. Call patching logic to regenerate SQL for the failed model.
4. Retry until success or `max_attempts` is reached.
5. If exhausted, mark model rejected and persist DLQ record.

Status outcomes:

- `SUCCESS`: all models pass.
- `PARTIAL`: at least one model passes, at least one rejected.
- `FAILURE`: no models pass or fatal setup failure.

## Checkpoint B (Manual Circuit Breaker)

Checkpoint B is triggered when a model exhausts automatic retry attempts. Instead of immediate DLQ routing, AMA writes a review artifact and pauses the model in `HITL_REQUIRED`.

### Checkpoint B artifact schema

```json
{
  "model_name": "orders_model",
  "current_sql": "select ...",
  "error_log": "Database Error in model orders_model ...",
  "attempt_history": [
    {
      "timestamp": "2026-03-25T14:01:10.000000+00:00",
      "error_snippet": "syntax error at or near \"from\"",
      "action_taken": "retry"
    },
    {
      "timestamp": "2026-03-25T14:01:44.000000+00:00",
      "error_snippet": "column does not exist: customer_segment_id",
      "action_taken": "checkpoint_b_generated"
    }
  ]
}
```

### CLI controls

Approve with fixed SQL:

```bash
# Bash / Git Bash
ama-ingest generate-dbt \
  --report sample_data/kfar_supply/kfar_report.json \
  --target-dialect duckdb \
  --dbt-project-dir . \
  --checkpoint-b-model orders_model \
  --approve-checkpoint-b ./fixes/my_model.sql
```

```powershell
# PowerShell (Windows)
ama-ingest generate-dbt `
  --report sample_data/kfar_supply/kfar_report.json `
  --target-dialect duckdb `
  --dbt-project-dir . `
  --checkpoint-b-model orders_model `
  --approve-checkpoint-b .\fixes\my_model.sql
```

Minimal form (approval path only):

```bash
# Bash / Git Bash
ama-ingest generate-dbt \
  --checkpoint-b-model orders_model \
  --approve-checkpoint-b ./fixes/my_model.sql
```

```powershell
# PowerShell (Windows)
ama-ingest generate-dbt `
  --checkpoint-b-model orders_model `
  --approve-checkpoint-b .\fixes\my_model.sql
```

Reject and route to DLQ:

```bash
# Bash / Git Bash
ama-ingest generate-dbt \
  --report sample_data/kfar_supply/kfar_report.json \
  --target-dialect duckdb \
  --reject-checkpoint-b orders_model
```

```powershell
# PowerShell (Windows)
ama-ingest generate-dbt `
  --report sample_data/kfar_supply/kfar_report.json `
  --target-dialect duckdb `
  --reject-checkpoint-b orders_model
```

When rejected from Checkpoint B, DLQ entries use:

- `error_stage: "CHECKPOINT_B_REJECTION"`

## Wave Gating and Orchestration

The execution orchestrator enforces strict wave barriers based on topological dependencies:

- Wave 0: Sources
- Wave 1: Staging
- Wave 2: Marts

Wave `N+1` is blocked until Wave `N` meets Definition of Ready.

### Definition of Ready (Wave N)

A wave is ready only when 100% of models in the wave are terminally acceptable:

- `SUCCESS`
- `PARTIAL`

Blocking states:

- `FIXING`
- `HITL_REQUIRED`
- `FAILED`

If a model reaches Checkpoint B (`HITL_REQUIRED`), the pipeline pauses after Wave `N` completion and waits for user action (`--approve-checkpoint-b` or `--bypass-wave`).

### Wave control flags

```bash
# Bash / Git Bash
ama-ingest generate-dbt \
  --report sample_data/kfar_supply/kfar_report.json \
  --target-dialect duckdb \
  --run-dbt \
  --bypass-wave 1
```

```powershell
# PowerShell (Windows)
ama-ingest generate-dbt `
  --report sample_data/kfar_supply/kfar_report.json `
  --target-dialect duckdb `
  --run-dbt `
  --bypass-wave 1
```

```bash
# Bash / Git Bash
ama-ingest generate-dbt \
  --report sample_data/kfar_supply/kfar_report.json \
  --target-dialect duckdb \
  --run-dbt \
  --stop-on-first-error
```

```powershell
# PowerShell (Windows)
ama-ingest generate-dbt `
  --report sample_data/kfar_supply/kfar_report.json `
  --target-dialect duckdb `
  --run-dbt `
  --stop-on-first-error
```

Bypass behavior:

- `--bypass-wave <wave_id>` skips the integrity gate for that wave.
- Warning log emitted:
  - `WARNING: Wave {wave_id} bypassed with incomplete models. Proceeding to Wave {wave_id + 1}.`

## React UI (primary)

Day-to-day migration ops use the **React dashboard**. See **[USER_GUIDE.md](../USER_GUIDE.md)** for page-by-page workflows.

Key API paths the React client calls:

| Action | Endpoint |
| --- | --- |
| Load report | `POST /report/load` |
| Per-table approve | `POST /migration/{report_id}/approve` |
| Bulk evaluate / run | `POST /scale/{report_id}/evaluate`, bulk WebSocket job |
| Cockpit Checkpoint-A start | `POST /cockpit/{report_id}/checkpoint-a/start` |
| Cockpit job poll | `GET /cockpit/checkpoint-a/job/{job_id}` |
| Cockpit approve + optional dbt | `POST /cockpit/checkpoint-a/job/{job_id}/approve` |
| Live extraction | `POST /api/live/start` |

### Per-table approve outcomes

`POST /migration/{report_id}/approve` returns one of:

| `status` | `success` | Meaning |
| --- | --- | --- |
| `approved` | `true` | Proposed SQL passed dbt validation; model written; audit decision recorded |
| `degraded_passthrough` | `false` | Validation failed; model replaced with `SELECT * FROM <table>` — review `original_sql` vs `final_sql`; **no** audit decision |
| `failed` | `false` | dbt validation failed with no acceptable fallback |

Response also includes `test_passed`, `stage1_error`, `stage2_error`, and `passthrough_used` when relevant.

## Legacy UI Usage Guide (Streamlit)

The Streamlit dashboard can orchestrate the dbt migration lifecycle without manual terminal steps.

### Using the Migration Agent Chat

The `Migration Agent` tab is a chat-first workflow for goal-oriented dbt migration with a mandatory human gate.

- First load shows only a welcome prompt and chat input (clean empty state).
- Project settings are anchored in the sidebar under **Project Configuration**.
- The target selector is labeled **Deployment Target Dialect**.
- The agent uses tools (`list_waves`, `analyze_schema`, `propose_dbt_model`, `execute_dbt_test`, `apply_fix`, `request_write_permission`) and pauses on write permission.
- Tool execution is surfaced as role-labeled collaboration steps:
  - **Architect** (`list_waves`, `analyze_schema`)
  - **Developer** (`propose_dbt_model`, `apply_fix`)
  - **QA Lead** (`execute_dbt_test`, `request_write_permission`)
- `request_write_permission` is equivalent to final Checkpoint B sign-off for file creation: no SQL is written until you click **Approve**.
- Chat output is table-focused: current-table tool output is shown inline, while unrelated prior-table details are collapsed.
- Duplicate gate noise is removed (`request_write_permission` is shown in the approval gate, not repeated in chat bubbles).
- **Default migration semantics: all rows.** For migration models, the system preserves full source-row coverage by default.
  - Business row filters (for example `WHERE status='unpaid'`) are treated as unsafe unless the user explicitly requests a filtered target dataset.
  - If an LLM draft introduces an unrequested business filter, AMA rejects that draft and falls back to deterministic all-rows SQL.
  - This prevents silent row-loss regressions during source-to-target migration.

Example conversation:

1. User: `Inspect the orders table and generate a model for Snowflake.`
2. Agent tools: `list_waves` -> `analyze_schema` -> `propose_dbt_model`.
3. UI shows SQL preview + mapping rows (when Hebrew translation exists) and waits for approval.
4. User clicks **Approve ✅**:
   - SQL is written to the configured model output directory.
   - `execute_dbt_test` runs automatically.
5. If test fails:
   - `apply_fix` runs automatically.
   - corrected SQL is returned to `request_write_permission` for re-approval.

### Where to start

1. Launch the dashboard: `ama-dashboard --report-path path/to/report.json`
2. Open the `Migration Agent` tab.
3. Choose target dialect and start with a prompt like: `Migrate Wave 1`.
4. Review Intelligence Feed + SQL draft, then use the bottom gate actions (`Approve ✅`, `Manual Edit ✏️`, `Skip ⏭️`) per model.
5. If using Manual Edit, click **Save Edited SQL** before approval.

#### dbt prerequisites
- `dbt_project.yml` must exist at the repo root (the app uses generated models under `models/ama_generated`).
- dbt requires a `profiles.yml` with at least one usable profile/target under `~/.dbt` (or the directory set by `DBT_PROFILES_DIR`).
- If `~/.dbt/profiles.yml` is missing, the runner will generate a minimal DuckDB template to unblock compilation; you should replace it with your real connection/credentials.

### Flow: Chat Intent -> Intelligence Feed -> Human Gate -> dbt Test/Fix

1. **User intent**: prompt the agent (for example, `Migrate all tables in wave 1`).
2. **Intelligence Feed**: tool outputs are rendered as structured tables (waves, schema, proposals, tests, fixes).
3. **Human Gate**: for each model, the agent must request write permission before creating/updating SQL on disk.
   - Gate is anchored at the bottom of the tab for consistent placement.
   - Manual edits are persisted through an explicit **Save Edited SQL** button.
4. **Execution loop**: after approval, dbt test runs automatically; on failure, Fix Agent proposes corrected SQL and returns to approval.
5. **Progress tracking**: wave progress is shown as `completed/total` with a progress bar (for example `1/2`), and successful approval auto-prepares the next table in the wave.

### Dashboard execution hardening (new)

The dashboard execution path now includes automatic safeguards for common local dbt issues so operators can execute from UI without terminal triage:

- **Target mismatch fallback**: if a requested `--target` is not configured in `profiles.yml` (for example `duckdb` requested but only `dev` exists), execution retries using the profile default target.
- **Legacy schema YAML sanitizer**: before dbt run/test, generated `*.schema.yml` files are repaired when unquoted `description` payloads would break YAML parsing.
- **Generated SQL sanitizer**: trailing semicolons in model SQL files are removed before dbt execution to avoid parser errors in wrapped adapter DDL contexts.
- **DuckDB lock retry**: transient `duckdb.db` lock failures are retried with bounded backoff instead of failing immediately.
- **Missing source auto-bootstrap**: for local DuckDB execution, missing `schema.table` source relations referenced by generated SQL are created best-effort so `dbt run`/`dbt test` can proceed. Column lists are merged from report DDL, `live_data/<connection>/ddl/*.json`, `schema.yml`, and model SQL; existing stub columns are never dropped on re-bootstrap. Cockpit batch execution pre-bootstraps all Checkpoint-A models before the first wave. Generated models are written to and executed from `dbt_project/models/ama_generated/`. This validates SQL locally only — it does not load production data.
- **Bulk migration visibility**: background bulk jobs are pre-registered (`queued`/`running`/`done`/`failed`), polled automatically in the UI, and completion state is reflected as migrated-table status.

### Legacy Notes

The previous `dbt Migration` ops-console flow (Checkpoint A/B screens and async generation jobs) remains documented for CLI/backward-compatibility context, but primary dashboard operations are now chat-driven through `Migration Agent`.

### Working with AI Agents

The `Migration Agent` tab exposes agentic generation and fix-loop metadata directly in the UI.

- **Agent badges**
  - `🤖 AI`: model SQL was accepted from the LLM path.
  - `⚙️ Legacy`: deterministic fallback was used for stability.
  - `User Modified`: manual SQL edits were made in the editor; this disables the AI badge for that model.
- **Confidence interpretation**
  - `< 60%`: high review risk, treat as manual-review-first.
  - `60–80%`: medium confidence, validate with targeted dbt tests.
  - `> 80%`: high confidence, still subject to checkpoint gating and wave dependencies.
- **Behind the Scenes reasoning**
  - Each model expander shows Schema Agent and dbt Agent reasoning so reviewers can understand selected columns, aliases, and cast choices.
- **Semantic Translation confidence**
  - Mapping rows include per-row confidence for Hebrew->English semantic translation.
  - Rows with confidence `< 0.8` are flagged as low confidence and contribute to `REVIEW_REQUIRED`.
- **`[TRANSLITERATION_WARNING]` in agentic mode**
  - This warning means semantic translation was not confidently resolved and transliteration fallback was retained.
  - Treat these rows as business-risk items that need glossary validation before production cutover.
- **Field-level mapping confidence**
  - Every mapping row includes per-row `confidence` and is visually flagged when `confidence < 0.8`.
  - `Source=Glossary` mappings are deterministic high confidence (typically `1.0`).
  - `Source=Transliteration` mappings use a lower baseline confidence (typically `0.55`) and carry `[TRANSLITERATION_WARNING]` for review.
- **`[DDL_ONLY_WARNING]` (always-show DDL columns)**
  - Some DDL columns may not be observed in the SQL logs used to derive the draft.
  - The UI still includes those columns in Checkpoint A mapping (so users can see the full DDL contract), but marks them with `[DDL_ONLY_WARNING]`.
  - Use these rows as a “coverage gap” indicator: they were pulled from the manifest/DDL rather than log evidence.
- **Wave intelligence & stress tests**
  - Each wave includes an “Executive AI Summary”.
  - The “Run AI Stress Test” button triggers the Scenario Agent across the wave and updates wave health + scenario ideas.
- **Risk meter & scenario test ideas**
  - The Risk Meter summarizes structural/performance drift risks and lists “agent concerns”.
  - Scenario test ideas should be converted into focused `dbt test` assertions.
- **Synthetic data augmentation (gated)**
  - Data-Gen runs only after explicit approval and a row-cap limit.
  - “View Synthetic Dataset” lets you inspect the `complex_mock_data.json` used for validation.
- **Chat-assisted patch proposals (HITL-only)**
  - “Chat with Model Agent” returns a SQL patch proposal.
  - Patch application is manual/HITL-only: the UI proposes, the operator decides.
- **Checkpoint B Fix-loop UI**
  - When models are in `HITL_REQUIRED`, the UI shows Fix Agent error analysis and a diff between failed SQL and the AI-suggested correction.
  - You can apply the AI fix (overwrite failing SQL) or route to DLQ after bounded retries.
- **Telemetry & cost dashboard**
  - The UI aggregates `tokens_used` per agent type and estimates total run cost.
  - It also shows agent performance stats (AI success rate, fix-it rate on first try) and respects fallback mode when the LLM is unavailable.

### AI Validation Flow

The AI Cockpit validation sequence is:

1. **Architect planning**: interpret source metadata, map Hebrew columns, and define migration intent.
2. **Developer draft**: generate dbt SQL/YAML for the configured target dialect.
   - Guardrail: default draft semantics preserve all source rows; unrequested business `WHERE` filters are blocked.
3. **QA syntax gate**: validate SQL with `sqlglot` before model review.
4. **Self-healing loop**: if QA rejects, call Developer self-correction (max 3).
5. **HITL escalation**: if still invalid, emit `CRITICAL_REASON` and require manual intervention.
6. **Risk Analysis**: Risk Agent rates model risk and surfaces concerns.
7. **Scenario & Synthetic Testing**: Scenario Agent generates test ideas; Data-Gen can generate `complex_mock_data.json` after explicit approval and row-cap checks.
8. **Fix-loop & Final Approval**: wave approval + dbt execution + Checkpoint B Fix-loop (diff + apply) when needed.

### Risk Score Interpretation

- **Low**: No major performance/drift/null concerns detected; proceed with normal tests.
- **Medium**: One or more concerns require targeted assertions (nulls, duplicates, joins, casts).
- **High**: Critical structural/performance risk; require manual review and explicit validation scenarios before promotion.

Operational guidance:

- Treat **High** as a release gate for that model/wave.
- Use Scenario Agent outputs to build focused dbt tests.
- Prefer manual review when Risk Agent is in fallback mode.

### Wave Tracker Screenshot Placeholder

`[Insert screenshot: Wave Progress Tracker with statuses (SUCCESS/RUNNING/HITL_REQUIRED/PENDING/FAILED)]`

### Checkpoint A Screenshot Placeholder

`[Insert screenshot: per-model SQL editor + mapping table with [TRANSLITERATION_WARNING] highlighting]`

### Checkpoint B Screenshot Placeholder

`[Insert screenshot: failing model error log + current SQL editor + attempt history timeline]`
