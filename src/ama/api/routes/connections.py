"""
Connection management routes.

POST /connections/test     — test a DB connection, return version + table count
POST /connections/explain  — run EXPLAIN on SQL and return optimizer plan
GET  /connections/health   — lightweight ping for monitoring
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/connections", tags=["Connections"])


class ConnectionTestRequest(BaseModel):
    mode: str                          # "file" | "postgres" | "oracle"
    connection_string: str | None = None
    manifest_path: str | None = None
    encrypted: bool = False            # True → decrypt with AMA_ENCRYPTION_KEY


class ConnectionTestResponse(BaseModel):
    ok: bool
    mode: str
    db_version: str | None = None
    tables_found: int = 0
    sample_tables: list[str] = []
    error: str | None = None


class ExplainRequest(BaseModel):
    sql: str
    mode: str = "file"
    connection_string: str | None = None
    encrypted: bool = False


@router.post("/test", response_model=ConnectionTestResponse)
def test_connection(body: ConnectionTestRequest) -> Any:
    """
    Test a DB connection. Used by the frontend onboarding wizard.
    Returns DB version, reachable table count, and first 5 table names.
    """
    from ama.mcp.factory import get_schema_provider

    try:
        provider = get_schema_provider(
            mode=body.mode,
            connection_string=body.connection_string,
            manifest_path=Path(body.manifest_path) if body.manifest_path else None,
            encrypted=body.encrypted,
        )

        if not provider.ping():
            return ConnectionTestResponse(
                ok=False,
                mode=body.mode,
                error="Provider ping() returned False — DB unreachable.",
            )

        # Get DB version if available
        db_version: str | None = None
        if hasattr(provider, "get_db_version"):
            db_version = provider.get_db_version()

        tables = provider.list_tables()
        return ConnectionTestResponse(
            ok=True,
            mode=body.mode,
            db_version=db_version,
            tables_found=len(tables),
            sample_tables=tables[:5],
        )

    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Connection test failed")
        return ConnectionTestResponse(ok=False, mode=body.mode, error=str(exc))


@router.post("/explain")
def explain_sql(body: ExplainRequest) -> dict[str, Any]:
    """
    Run the DB's native EXPLAIN on a SQL statement.
    Used by the Self-Healing validation loop.
    Returns {"ok": bool, "plan": str, "error": str | None, "dialect": str}
    """
    from ama.mcp.factory import get_schema_provider

    try:
        provider = get_schema_provider(
            mode=body.mode,
            connection_string=body.connection_string,
            encrypted=body.encrypted,
        )
        result = provider.execute_explain(body.sql)
        return {
            "ok": result.ok,
            "plan": result.plan,
            "error": result.error,
            "dialect": result.dialect,
        }
    except Exception as exc:
        return {"ok": False, "plan": "", "error": str(exc), "dialect": body.mode}


@router.get("/health")
def connection_health(
    mode: str = "file",
    connection_string: str | None = None,
) -> dict[str, Any]:
    """Lightweight liveness check for monitoring."""
    from ama.mcp.factory import get_schema_provider

    try:
        provider = get_schema_provider(mode=mode, connection_string=connection_string)
        alive = provider.ping()
        return {"ok": alive, "mode": mode}
    except Exception as exc:
        return {"ok": False, "mode": mode, "error": str(exc)}

