# Local SQL Server Setup (Docker + ODBC)

This guide helps developers run a local SQL Server instance in Docker and connect the app (API container) via ODBC.

The #1 gotcha is **networking**:
- **From your host machine**, `SERVER=localhost` points to your host.
- **From inside the API container**, `SERVER=localhost` points to the container itself (NOT SQL Server).

So for the API container to connect reliably, set `SERVER=` to the **SQL Server container IP** (or a shared Docker network alias).

## 1. Prerequisites

### ODBC Driver (required)

Use **ODBC Driver 18 for SQL Server**.

Download (official Microsoft page):
https://learn.microsoft.com/en-us/sql/connect/odbc/download-odbc-driver-for-sql-server

Verify the driver name on Windows (optional):
- Open “ODBC Data Sources (64-bit)” → “Drivers”
- Confirm the exact string is `ODBC Driver 18 for SQL Server`

### Docker

You need Docker Desktop or a Docker Engine environment capable of running containers locally.

## 2. One-time / Local Initialization Workflow (recommended)

This repo includes a helper that does the “right thing” for local dev.

1. Set a SQL Server `sa` password in your environment (do not commit the password to git).
2. Run the local bootstrap script:

```bash
python tools/setup_dev_mssql.py
```

This script:
- creates/starts a Docker container named `ama-mssql-dev` (idempotent)
- waits for SQL Server readiness
- recreates the `kfar_supply` database
- injects schema from the repository’s demo DDL artifacts
- seeds seed data for the Kfar Supply demo context
- updates the local `.env` with:
  - `MSSQL_CONNECTION_STRING` (for convenience)
  - a `SERVER=` value that is reachable **from the API container**

## 3. Connectivity Nuances (ODBC 18 Specifics)

ODBC Driver 18 enforces encryption options more strictly than older drivers.

Symptom -> Cause -> Solution:

### Encryption (`Encrypt=yes`)

**Symptom:** Connection fails or aborts during TLS negotiation.

**Cause:** ODBC Driver 18 typically requires encryption parameters.

**Solution:** Ensure the connection string includes:
- `Encrypt=yes`

### Trusting the server certificate (`TrustServerCertificate=yes`)

**Symptom:** SSL Provider errors related to certificate chain trust.

**Cause:** The Docker container uses a self-signed certificate (not trusted by default).

**Solution:** Ensure the connection string includes:
- `TrustServerCertificate=yes`

## 4. Connection String (DSN-less)

### 4.1 Template

Use a DSN-less ODBC connection string template like:

```text
DRIVER={ODBC Driver 18 for SQL Server};SERVER=<SQLSERVER_CONTAINER_IP>;DATABASE=kfar_supply;UID=sa;PWD=<password>;Encrypt=yes;TrustServerCertificate=yes;
```

Notes:
- The trailing `;` is optional but harmless.
- Use the exact driver name.

### 4.2 How to get `<SQLSERVER_CONTAINER_IP>`

From the host:

```bash
docker inspect ama-mssql-dev --format "{{json .NetworkSettings.Networks}}"
```

Pick the `IPAddress` under the `bridge` network (commonly something like `172.17.0.2`).

Notes:
- Do not hardcode credentials in git.
- Keep secrets in local `.env` only.

## 5. Environment Variables

The app’s MCP provider selection uses `AMA_SCHEMA_MODE` and `AMA_DB_CONNECTION_STRING`.

| Variable | Value/Description |
| :--- | :--- |
| `AMA_SCHEMA_MODE` | Must be set to `sqlserver` |
| `AMA_DB_CONNECTION_STRING` | Full ODBC connection string (DSN-less), used by the API container to connect |
| `AMA_DB_TIMEOUT` | Optional (seconds). Default `10` (or whatever your env sets). Increase if your Docker is slow. |

Note: For local Docker SQL Server with self-signed certificates, include `Encrypt=yes` and `TrustServerCertificate=yes` in the connection string.

### 5.1 Minimal working `.env` (example)

```dotenv
AMA_SCHEMA_MODE=sqlserver
AMA_DB_TIMEOUT=30
AMA_DB_CONNECTION_STRING="DRIVER={ODBC Driver 18 for SQL Server};SERVER=<SQLSERVER_CONTAINER_IP>;DATABASE=kfar_supply;UID=sa;PWD=<password>;Encrypt=yes;TrustServerCertificate=yes;"
```

If you ran `python tools/setup_dev_mssql.py`, it should have already written a working `SERVER=` for you.

## 6. Running the API container with updated env

Docker Compose loads `.env` **when the container is created**.

- If you change `.env`, **recreate** the API container:

```bash
docker compose up -d --force-recreate api
```

- A plain `docker restart ...` typically **will not** reload `.env` values.

## 7. Quick smoke tests

### 7.1 Health check

```bash
curl http://localhost:8000/health
```

Should return:

```json
{"status":"ok"}
```

### 7.2 Discovery: list tables (SQL Server)

The Discovery API uses **POST** (not GET) to avoid leaking credentials in URL query strings.

```bash
curl -X POST http://localhost:8000/api/discovery/tables ^
  -H "Content-Type: application/json" ^
  -d "{\"mode\":\"sqlserver\",\"schema_filter\":\"dbo\"}"
```

If your API is not configured via `.env` and you want to test with an explicit connection string, include `connection_string` in the JSON body.

## 8. Troubleshooting (Error Codes)

### `IM002`

```text
IM002: Data source name not found and no default driver specified.
```

Symptom -> Cause -> Solution:
- **Symptom:** ODBC cannot connect; driver name errors.
- **Cause:** ODBC Driver 18 is not installed, or the driver name in your connection string does not match what’s installed.
- **Solution:** Install **ODBC Driver 18 for SQL Server** and ensure the connection string uses the exact driver name.

### `08001`

```text
08001: SSL Provider: The certificate chain was issued by an authority that is not trusted.
```

Symptom -> Cause -> Solution:
- **Symptom:** TLS/certificate validation fails.
- **Cause:** The SQL Server container uses a certificate not trusted by the local client.
- **Solution:** Add `TrustServerCertificate=yes` to the connection string (and keep `Encrypt=yes` for Driver 18).

### `HYT00` (Login timeout expired)

```text
HYT00: [Microsoft][ODBC Driver 18 for SQL Server]Login timeout expired
```

Most common causes:
- **Wrong `SERVER=` from inside the API container**
  - Fix: use the SQL Server container IP (or a network alias), not `localhost`.
- **Container not ready yet**
  - Fix: wait ~10–30s after first start; or re-run `python tools/setup_dev_mssql.py`.
- **Firewall/VPN interference**
  - Fix: temporarily disable VPN / security software that blocks local Docker networking.

## Live connection exports (`live_data/`)

Full reference: **[LIVE_CONNECTION.md](LIVE_CONNECTION.md)** (source modes, API, Query Store, troubleshooting).

The **Live connection** UI writes files under:

```text
<AMA project root>/live_data/<connection_name>/
```

The project root is resolved from `src/ama/config.py` by walking up to `pyproject.toml`, unless you override it.

- **If files do not appear in your Git clone:** the API process may be using a different root (Docker `/app`, or a non-editable install under `site-packages`). Set an explicit root in `.env`:

```env
AMA_PROJECT_ROOT=C:\path\to\Autonomous-Migration-Architect-AMA
```

Restart the API, run ingestion again, then check `live_data\<connection_name>\` under that folder.

- The job log in the UI now includes a line **`Full artifact path: ...`** with the resolved directory.

### Docker Compose (`docker compose up`)

The API service uses **`/app`** as the project root. **`live_data/` is bind-mounted** from your clone:

```text
./live_data  →  /app/live_data  (inside the api container)
```

So exports appear on the host at **`<repo>/live_data/<connection_name>/`**. If you started Compose before this mount existed, run **`docker compose up -d --build`** (or recreate the `api` service) so the volume is applied.

## Live connection: build report + auto-load in UI

The Live connection page supports two **source modes** (see [LIVE_CONNECTION.md](LIVE_CONNECTION.md)):

- **Kfar Demo (synthetic)** — deploys demo schema/DML; report includes bundled Kfar glossary/comms/git from `sample_data/kfar_supply`.
- **Real Extraction (read-only)** — extracts real DDL + SQL logs; **`all_schemas`** exports every user BASE TABLE; or set **`schemas`** (e.g. `dbo, finance, logistics`); report uses **only** files under `live_data/<connection_name>/` (no demo glossary).

**Schema scope**

| UI / API | DDL exported | SQL log filter |
| --- | --- | --- |
| **All user schemas** (`all_schemas: true`) | Every BASE TABLE (excludes `sys`, `INFORMATION_SCHEMA`, `guest`) | All application SQL |
| **Schemas list** (default `dbo`) | Tables in listed schemas only | Queries referencing any listed schema |
| Omitted | Defaults to `dbo` only | `dbo` references only |

**Tables tab:** lineage graph shows PK/FK arrows plus shared-query counts from SQL logs; nodes show per-table query counts.

To seed Query Store / plan cache with application SQL, run [`tools/kfar_test_queries.sql`](../tools/kfar_test_queries.sql) in SSMS against the **same server** as `AMA_DB_CONNECTION_STRING`, then re-run Real Extraction. Job logs include `Connected to server=… database=…` and dedupe stats (`unique_after_dedupe`).

If the checkbox appears enabled but job logs show `build_report=false`, your running API image is stale. Rebuild API and web:

```bash
docker compose build api web
docker compose up -d api web
```

Shared UI options:

- **Build AMA report after export** — runs ingestion over `live_data/<connection_name>/` and writes `ama_live_report.json`
- **When ready, load that report in this UI and open Tables** — auto-calls `/report/load` with the generated path

## Scale/bulk defaults for Kfar live dataset

Current API defaults are:

- Scale evaluate: `conf_floor=70`, `crit_ceil=40`
- Bulk start: `conf_floor=70`, `crit_ceil=40`

This alignment ensures **Evaluate** and **Bulk** use the same queue thresholds.

Also, bulk scope is constrained to the DDL manifest: tables not listed in `ddl_manifest_table_keys` are BLOCKed with `outside_manifest_scope` and cannot be queued as GREEN (for example, `legacy_hebrew.*` / technical staging tables).

