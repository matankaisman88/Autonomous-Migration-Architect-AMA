# Live Connection (SQL Server)

The **Live connection** page in the React UI (`/live`) connects to a SQL Server database, exports migration inputs under `live_data/<connection_name>/`, and optionally builds an AMA JSON report in the same folder.

**API endpoint:** `POST /api/live/start` (returns `{ "job_id", "connection_name", "build_report" }`)

**Progress:** WebSocket `ws://<api>/ws/live/{job_id}`

See also: [SQLSERVER.md](SQLSERVER.md) for ODBC setup, Docker networking, and local bootstrap.

## Source modes

| Mode | UI label | Writes to DB? | SQL logs | DDL | Report extras |
| --- | --- | --- | --- | --- | --- |
| `kfar_demo` (default) | **Kfar Demo (synthetic)** | **Yes** — deploys Kfar Supply demo tables + seed DML | Synthetic JSONL (`build_jsonl_lines`) | Hardcoded Kfar column JSON from `ama.kfar_supply.spec` | Bundled `sample_data/kfar_supply` glossary, comms, git SQL |
| `real_extract` | **Real Extraction (read-only)** | **No** — read-only introspection only | Query Store → plan-cache fallback | `INFORMATION_SCHEMA` (BASE TABLEs) | **Only** exported artifacts (no bundled glossary/comms/git) |

Use **`kfar_demo`** for the controlled Hebrew ↔ English demo narrative.

Use **`real_extract`** against a real or shared SQL Server when you want evidence from the database itself.

> **Phase 1:** `real_extract` requires `mode=sqlserver` (400 otherwise). Oracle/DB2 live extraction is planned via the same `SchemaProvider` duck-typing hooks.

## Artifact layout

Both modes write the same directory shape so downstream `cmd_run` / discovery merge works unchanged:

```text
live_data/<connection_name>/
  ddl/<schema>_<table>.json     # {"columns": ["col", ...]}
  manifest.json                 # schema.table -> ddl/relative path
  sql_logs/prod.jsonl           # one JSON object per line: env, dialect, sql
  ama_live_report.json          # when build_report=true
```

Real-extraction manifests include `_extraction_meta` (log source, schemas, warnings, row counts). Keys starting with `_` are ignored by the DDL manifest loader.

## Real extraction details

### DDL (`extract_ddl`)

- **All schemas:** set `all_schemas: true` to export every user **BASE TABLE** in the database (excludes `sys`, `INFORMATION_SCHEMA`, `guest`). SQL logs are not schema-filtered in this mode.
- **Specific schemas:** request field `schemas` (default **`["dbo"]`** when neither `all_schemas` nor `schemas` is set).
- Table types: **BASE TABLE** only (views excluded).
- Zero tables → job **failure** (no partial manifest).

### SQL logs (`extract_logs`)

1. **Query Store** (if enabled): `sys.query_store_*` filtered by date range.
2. **Fallback:** `sys.dm_exec_query_stats` + `sys.dm_exec_sql_text()` (plan cache only; dates ignored).
3. Filters out system/noise SQL (`sys.*`, `sp_*`, `SET SHOWPLAN`, etc.).
4. Keeps queries that reference requested schemas (e.g. `dbo.`).
5. **Literal redaction** before write: string literals → `'<REDACTED>'`, comparison numerics → `<N>`.
6. Dedupe on pre-redaction text; pool size up to 20× `max_log_rows` (min 1000).

**Defaults when dates omitted:** last **7 calendar days** through today (Query Store path only).

**Permissions (typical):** `VIEW DATABASE STATE` for Query Store; `VIEW SERVER STATE` for plan cache (instance-level).

### Report build (`real_extract`)

- Uses only `live_data/<connection_name>/` artifacts.
- Does **not** attach `sample_data/kfar_supply/glossary/*.json`, comms, or git SQL.
- Glossary UI shows mappings from **logs + DDL merge** only.
- Report anchor: optional `migration_context` (`schema.table`), else first manifest table alphabetically.

Job log line when skipping demo context:

```text
real_extract: skipping bundled Kfar glossary/comms/git sample_data
```

## API request examples

### Kfar demo (backward compatible)

```json
{
  "mode": "sqlserver",
  "connection_name": "demo",
  "host": "172.17.0.2",
  "port": 1433,
  "user": "sa",
  "password": "***",
  "database": "kfar_supply",
  "jsonl_lines": 1200,
  "build_report": true
}
```

### Real extraction (specific schemas)

```json
{
  "mode": "sqlserver",
  "connection_name": "prod-crm",
  "connection_string": "DRIVER={ODBC Driver 18 for SQL Server};SERVER=...;DATABASE=...;UID=...;PWD=...;TrustServerCertificate=yes;",
  "source_mode": "real_extract",
  "schemas": ["dbo", "finance", "logistics"],
  "log_start_date": "2026-07-01",
  "log_end_date": "2026-07-21",
  "max_log_rows": 10000,
  "build_report": true,
  "migration_context": "dbo.orders"
}
```

### Real extraction (entire database)

```json
{
  "mode": "sqlserver",
  "connection_name": "kfar-full",
  "connection_string": "...",
  "source_mode": "real_extract",
  "all_schemas": true,
  "build_report": true
}
```

| Field | Default | Notes |
| --- | --- | --- |
| `source_mode` | `kfar_demo` | |
| `all_schemas` | `false` | Real extract: every user BASE TABLE; mutually exclusive with `schemas` |
| `schemas` | `["dbo"]` | Real extract only when `all_schemas` is false |
| `log_start_date` / `log_end_date` | last 7 days → today | ISO `YYYY-MM-DD` |
| `max_log_rows` | `10000` | `1`–`50000` |
| `migration_context` | first manifest table | Optional report anchor |
| `jsonl_lines` | `1200` | Kfar demo only |

## UI workflow

1. Open **Live connection** in the React app (`http://localhost:3000` with Docker).
2. Choose dialect **sqlserver** and enter connection details (or paste ODBC connection string).
3. Select **Kfar Demo** or **Real Extraction**.
   - Real extract: check **All user schemas** for full DB, or enter `dbo, finance, logistics` in Schemas.
4. Optional: **Build AMA report after export** and **auto-load** into Tables view.
5. **Test connection** → **Start ingestion** → watch WebSocket progress.

After changing backend or frontend Live code, rebuild:

```bash
docker compose build api web && docker compose up -d
```

PowerShell:

```powershell
docker compose build api web; docker compose up -d
```

## Populating Query Store for testing

Real extraction only captures SQL that actually ran. To seed application queries against Kfar `dbo` tables:

1. Open [`tools/kfar_test_queries.sql`](../tools/kfar_test_queries.sql) in SSMS.
2. Set `USE <your_database>;` if not `kfar_supply`.
3. Execute all batches (F5). Each batch has a unique `/* ama-test-qNNN */` comment so dedupe keeps distinct entries.
4. Re-run **Real Extraction** with log end date = today.

## Docker / paths

- Host: `<repo>/live_data/<connection_name>/`
- API container: `/app/live_data/<connection_name>/` (bind mount in `docker-compose.yml`)
- Override project root: `AMA_PROJECT_ROOT` in `.env` if exports land in an unexpected folder.

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| Empty or sparse `prod.jsonl` | No app SQL in Query Store / plan cache for date range | Run workloads; widen dates; use `tools/kfar_test_queries.sql` |
| Only system-catalog SQL | Query Store dominated by tooling queries | Fixed by application-SQL filter; re-run after app queries |
| **Glossary inventory** Hebrew terms on real extract | Stale API before glossary skip fix | Rebuild API; use `real_extract` (not demo glossary) |
| Only 3 DDL tables on Kfar | Default `schemas: ["dbo"]` only | Enable **All user schemas** or add `finance, logistics` |
| Log row count unchanged after re-run | Dedupe by SQL text | Run **new distinct** SQL; check `unique_after_dedupe` in job log |
| `build_report=false` in logs | Stale API image | Rebuild `api` and `web` services |
| `no BASE TABLEs found in schemas: dbo` | Wrong DB or empty schema | Check connection database; list tables in SSMS |
| Connection timeout from API container | `SERVER=localhost` inside container | Use SQL Server container IP — see [SQLSERVER.md](SQLSERVER.md) |
