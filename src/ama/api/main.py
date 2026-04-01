from __future__ import annotations

from contextlib import asynccontextmanager
import json
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ama.api.live_jobs import mark_all_live_jobs_shutdown
from ama.api.routes import agent, analytics, bulk, cockpit, dq, hitl, ingest, migration, planner, report, scale
from ama.api.routes.connections import router as connections_router
from ama.api.routes.discovery import router as discovery_router
from ama.api.routes.live_connection import router as live_router
from ama.api.ws import router as ws_router
from ama.api.routes import mcp as mcp_router


def _is_truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _cors_origins_from_env() -> list[str]:
    """
    Resolve CORS origins from env.
    - Prefer CORS_ORIGINS (JSON array or comma-separated list)
    - Fallback to ["*"] only when DEBUG is true
    - Otherwise default to localhost dev origins
    """
    raw = (os.getenv("CORS_ORIGINS") or "").strip()
    if raw:
        if raw.startswith("["):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    cleaned = [str(v).strip() for v in parsed if str(v).strip()]
                    if cleaned:
                        return cleaned
            except json.JSONDecodeError:
                pass
        cleaned = [part.strip() for part in raw.split(",") if part.strip()]
        if cleaned:
            return cleaned

    if _is_truthy(os.getenv("DEBUG")):
        return ["*"]
    return ["http://localhost:3000", "http://127.0.0.1:3000"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Track startup/shutdown hooks and mark running jobs failed on shutdown."""
    _ = app
    yield
    from ama.bulk_runner import _BULK_JOBS, _BULK_JOBS_LOCK

    with _BULK_JOBS_LOCK:
        for job in _BULK_JOBS.values():
            if job.get("status") == "running":
                job["status"] = "failed"
                job["error"] = "Server shutdown"
    mark_all_live_jobs_shutdown()


app = FastAPI(
    title="AMA — Autonomous Migration Architect API",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins_from_env(),
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(report.router, prefix="/report", tags=["Report"])
app.include_router(scale.router, prefix="/scale", tags=["Scale Engine"])
app.include_router(migration.router, prefix="/migration", tags=["Migration"])
app.include_router(bulk.router, prefix="/bulk", tags=["Bulk"])
app.include_router(agent.router, prefix="/agent", tags=["Agent"])
app.include_router(planner.router, prefix="/planner", tags=["Planner"])
app.include_router(analytics.router, prefix="/analytics", tags=["Analytics"])
app.include_router(hitl.router, prefix="/hitl", tags=["HITL"])
app.include_router(dq.router, prefix="/dq", tags=["Data Quality"])
app.include_router(cockpit.router, prefix="/cockpit", tags=["DBT Cockpit"])
app.include_router(ingest.router, prefix="/ingest", tags=["Ingest"])
app.include_router(ws_router, tags=["WebSocket"])
app.include_router(connections_router, prefix="/api", tags=["Connections"])
app.include_router(discovery_router, prefix="/api", tags=["Discovery"])
app.include_router(live_router, prefix="/api", tags=["Live"])
app.include_router(mcp_router.router, prefix="/mcp")


@app.get("/health", tags=["Health"])
def health() -> dict:
    return {"status": "ok"}


def start() -> None:
    import uvicorn

    uvicorn.run("ama.api.main:app", host="0.0.0.0", port=8000, reload=True)

