# Contributing

| Area | Notes |
|------|--------|
| **Install** | `pip install -e ".[dev]"` from the repo root |
| **Tests** | `pytest tests/ -q` (307 tests as of last count) |
| **Style** | Match existing modules; keep changes small and focused |
| **Docs** | Update `README.md` (technical), `USER_GUIDE.md` (operators), or `MIGRATION.md` / `docs/*.md` when behavior changes |

Key doc entry points:

- **[USER_GUIDE.md](USER_GUIDE.md)** — React UI workflows
- **[MIGRATION.md](MIGRATION.md)** — CLI, Checkpoint A/B, DLQ
- **[docs/LIVE_CONNECTION.md](docs/LIVE_CONNECTION.md)** — real SQL Server extraction
- **[docs/SQLSERVER.md](docs/SQLSERVER.md)** — local dev fixture + ODBC

Do not commit generated logs, Excel exports, or `chaos_data/` outputs (see `.gitignore`).
