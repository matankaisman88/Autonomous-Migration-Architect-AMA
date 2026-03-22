# AMA — User guide (non-technical)

| Audience | Managers, analysts, sponsors |
|------------|------------------------------|
| **Technical setup** | See **`README.md`** |

---

## First 5 minutes

| Question | Answer |
|----------|--------|
| **What do I get?** | An **Excel** workbook (`.xlsx`) and/or a **JSON** report from your engineering team |
| **Where is the Excel file?** | Shared folder or artifact store your team uses; names often like `ama_report_<table>_<timestamp>.xlsx` |
| **How do I open it?** | **Microsoft Excel** or any compatible spreadsheet app |
| **Optional UI** | **Streamlit** dashboard — your team provides a **JSON path** or runs the app; sidebar **Report path** or upload |

---

## Excel & dashboard — how to read outputs

### Impact vs readiness (executive)

| Zone | Meaning |
|------|---------|
| **High importance + high readiness** | Strong candidates to schedule **early** in migration waves |
| **High importance + lower readiness** | Valuable but expect **more design / cleanup** |
| **Lower importance** | Often **defer** (unless compliance needs it) |

Use this to **sequence work**, not as the only go/no-go.

### Confidence colors

| Color | Meaning for the business |
|-------|---------------------------|
| **Green** | Strong evidence (glossary/exact/strong blend) — **planning-ready** per your governance |
| **Yellow** | Possible mapping — usually needs **human review** before calling it final |
| **Red** | Weak / unsafe for cutover — often **review** or **trash** in technical sheets |

Confidence is **technical evidence**, not a legal guarantee.

### Portfolio filter

| Filter | Use |
|--------|-----|
| **Technical Debt** | Isolate scratch/temp/low-continuity objects — plan **separately** from core cutover |

“Technical debt” here does **not** mean “delete data” — it means **different milestone**.

---

## Dashboard — quick steps

### Business Translator (Glossary tab)

| Step | Action |
|------|--------|
| 1 | Open **Business Glossary** |
| 2 | Use **Filter glossary** (Hebrew/English, table names, targets) |
| 3 | Read the **summary table** |
| 4 | **Expand** rows for full text, confidence gauge, affected tables |

### HITL (Review tab)

| Step | Action |
|------|--------|
| 1 | Open **Review (HITL)** |
| 2 | Review each **legacy → suggested DDL** row |
| 3 | **Approve** or **Reject** |
| 4 | Decisions save to **`<report>.hitl.json`** when using a file path (not upload-only) |

| After approvals | Ask engineering to **merge HITL** into a new Excel/JSON so **Migration** shows confirmed rows — `ama-ingest apply-hitl` |

---

## FAQ

| Term | Meaning |
|------|---------|
| **Trash** | Low-trust tokens kept **out** of the confirmed list — **not** “bad people,” **don’t auto-trust** this mapping |
| **Unmapped** | Legacy name did **not** merge to DDL this run — may still matter for volume/comms; may need glossary or manual model |
| **Hebrew looks odd in a terminal** | Display quirk; **Excel** and the **dashboard** are authoritative |
| **Numbers look wrong** | Give **examples** (table, column, sheet name) to engineering for re-run or config changes |

---

## Operators — benchmarks & stress

| Goal | Command | Output |
|------|---------|--------|
| **Benchmark** (10k / 50k / 100k Tier-5 rows) | `ama-ingest run --benchmark` | `benchmark_results.json` |
| **Extreme stress** (million-row style logs) | Generate with `python tools/generate_extreme_chaos.py`, then `ama-ingest run --stress` | `stress_report.json` |

| Override | Env / flag |
|----------|------------|
| Benchmark path | `--benchmark-results PATH` |
| Stress log | `AMA_STRESS_LOG` or default under `chaos_data/sql_logs/` |
| Cap records | `AMA_STRESS_MAX_LINES` or `--stress-lines` |

Full million-row **parsing** of very heavy SQL can take **hours** depending on hardware.

---

*AMA — Autonomous Migration Architect*
