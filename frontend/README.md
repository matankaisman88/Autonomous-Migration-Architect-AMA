# AMA React Frontend

Minimal React app wired to the AMA FastAPI backend. For full operator workflows, see **[USER_GUIDE.md](../USER_GUIDE.md)**.

## Prerequisites

- Node.js 18+
- AMA API running locally on `http://localhost:8000`

## Run

```bash
cd frontend
npm install
npm run dev
```

Then open `http://localhost:5173`.

With Docker Compose, the built app is served at `http://localhost:3000`.

## Configure

Create `frontend/.env.local` (or `.env`):

```bash
VITE_AMA_API_BASE=http://localhost:8000
VITE_DEFAULT_REPORT_PATH=../sample_data/kfar_supply/kfar_report.json
```

## App sections

| Route | Page |
| --- | --- |
| `/` | Overview |
| `/tables` | Tables |
| `/live` | Live connection |
| `/glossary` | Glossary |
| `/bulk` | Bulk Migration |
| `/planner` | Planner |
| `/hitl` | HITL |
| `/dq` | Data Quality |
| `/cockpit` | DBT Cockpit |
| `/agent` | Migration Agent |
