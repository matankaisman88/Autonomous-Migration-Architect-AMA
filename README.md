# Autonomous Migration Architect (AMA)

| | |
|---|---|
| **Purpose** | Legacy-to-cloud migration intelligence from **SQL logs**, **Git SQL**, and **comms** |
| **Outputs** | JSON / Markdown / **Excel** reports and a **Streamlit** dashboard |
| **Python** | 3.11+ |

---

## What AMA does

| Layer | Role |
|-------|------|
| **Ingestion** | Sanitize text (NFC, control chars), parse with **SQLGlot**, **regex fallback** when parsing fails |
| **Alias resolution** | Map log columns → target **DDL** via glossary → exact match → lexical + hash embeddings; **merge floor** / **confirmed threshold** protect quality |
| **Reporting** | Importance, discovery inventory, **Migration** sheet, **HITL** review queue |

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
| **Executive overview** | KPIs, impact vs readiness scatter, domain importance |
| **Domains** | Per-domain health and inventory |
| **Business Glossary** | Grouped legacy → DDL stories |
| **Ask the data** | Concept search (Hebrew/English) |
| **Tables** | Per-table merge breakdown |
| **Review (HITL)** | Approve/reject; sidecar `<report>.hitl.json` |

**HITL → Excel:** approvals are not auto-written into the JSON report. Merge sidecar into a new artifact:

```bash
ama-ingest apply-hitl --report report.json --format excel -o report.with_hitl.xlsx
```

---

## Install & run

```bash
pip install -e .
# optional: heavier embeddings
pip install -e ".[embed]"
```

| Command | Action |
|---------|--------|
| `ama-ingest run --help` | Full ingestion (SQL logs, comms, Git, importance) |
| `ama-ingest run --format json -o report.json` | JSON report |
| `ama-ingest run --format excel -o report.xlsx` | Excel workbook |
| `ama-ingest apply-hitl --report report.json` | Apply `.hitl.json` → merged JSON/Excel |
| `ama-ingest run --benchmark` | Performance benchmark → `benchmark_results.json` |
| `ama-ingest run --stress` | Extreme log stress → `stress_report.json` |
| `ama-dashboard --report-path report.json` | Streamlit UI |

---

## Environment variables (`AMA_*`)

| Variable | Purpose |
|----------|---------|
| `AMA_TARGET_SCHEMA` / `AMA_TARGET_TABLE` | Default focus table |
| `AMA_DEFAULT_DB` | Catalog when logs use `schema.table` only |
| `AMA_MERGE_CONFIDENCE_FLOOR` | Merge floor (default `0.4`) |
| `AMA_MERGE_CONFIRMED_THRESHOLD` | Vector confirm bar (default `0.8`) |
| `AMA_DDL_COLUMNS_PATH`, `AMA_GLOSSARY_PATH`, `AMA_SQL_LOGS_GLOB`, `AMA_COMMS_DIR`, `AMA_GIT_SQL_ROOTS` | Paths / globs |
| `AMA_QDRANT_PATH` | Optional on-disk Qdrant |
| `AMA_OPENAI_API_KEY` / `OPENAI_API_KEY` | Optional narrative enrichment |
| `AMA_REPORT_PATH` | Default report path for dashboard |

Load from **`.env`** in the working directory when present.

---

## Repository layout

| Path | Role |
|------|------|
| `src/ama/` | Library and CLI |
| `tests/` | Pytest suite |
| `sample_data/` | Small fixtures for CI and demos |
| `tools/` | Generators (`generate_*`, stress helpers) |
| `USER_GUIDE.md` | Non-technical reader guide |
| `CONTRIBUTING.md` | Dev quick reference |

**`chaos_data/`** is **gitignored** (generated). Create it with:

```bash
python tools/generate_full_db_chaos.py
python tools/generate_extreme_chaos.py --lines 1000000 --out chaos_data/sql_logs/extreme_1m.jsonl
```

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
