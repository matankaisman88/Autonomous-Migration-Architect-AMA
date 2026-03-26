# AMA React Frontend

Minimal React app wired to the AMA FastAPI backend.

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

## Configure API base URL

Create `.env` in `frontend/`:

```bash
VITE_AMA_API_BASE=http://localhost:8000
```

## Implemented app sections

- Overview
- Tables
- Bulk Migration
- Planner
- HITL
- Data Quality
- DBT Cockpit
- Migration Agent
