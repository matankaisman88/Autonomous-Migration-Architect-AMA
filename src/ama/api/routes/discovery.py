"""
Discovery routes.

POST /discovery/tables     — list tables from live DB (or file manifest)
POST /discovery/schema     — get full column schema for one table
POST /discovery/sample     — get PII-masked sample rows for one table

Security note:
  `connection_string` is accepted only in request bodies (not URL query params),
  to avoid leaking credentials into load balancer / CDN access logs.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/discovery", tags=["Discovery"])


class DiscoveryTablesRequest(BaseModel):
    mode: str = "file"  # file | postgres | oracle
    connection_string: str | None = None
    encrypted: bool = False
    schema_filter: str | None = None  # optional schema/owner name


class DiscoverySchemaRequest(BaseModel):
    mode: str = "file"  # file | postgres | oracle
    connection_string: str | None = None
    encrypted: bool = False
    table_key: str  # "schema.table"


class DiscoverySampleRequest(BaseModel):
    mode: str = "file"  # file | postgres | oracle
    connection_string: str | None = None
    encrypted: bool = False
    table_key: str  # "schema.table"
    limit: int = 5  # safe default


@router.get("/tables")
def _legacy_list_tables_get() -> None:
    raise HTTPException(
        status_code=405,
        detail="Use POST /api/discovery/tables with a request body (connection_string must not be in URL query params).",
    )


@router.post("/tables")
def list_tables(body: DiscoveryTablesRequest) -> dict[str, Any]:
    """
    Pull table list directly from the selected DB (or file manifest).
    Replaces manual table name entry in the onboarding UI.
    """
    try:
        from ama.mcp.factory import get_schema_provider

        provider = get_schema_provider(
            mode=body.mode,
            connection_string=body.connection_string,
            encrypted=body.encrypted,
        )
        tables = provider.get_table_list(schema_filter=body.schema_filter)
        return {
            "mode": body.mode,
            "schema_filter": body.schema_filter,
            "count": len(tables),
            "tables": tables,
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Discovery list_tables failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/schema/{table_key:path}")
def _legacy_get_table_schema_get(table_key: str) -> None:
    raise HTTPException(
        status_code=405,
        detail="Use POST /api/discovery/schema with a request body (connection_string must not be in URL query params).",
    )


@router.post("/schema")
def get_table_schema(body: DiscoverySchemaRequest) -> dict[str, Any]:
    """
    Return full column metadata for a single table.
    table_key format: schema.table  e.g. sales.orders
    """
    try:
        from ama.mcp.factory import get_schema_provider

        provider = get_schema_provider(
            mode=body.mode,
            connection_string=body.connection_string,
            encrypted=body.encrypted,
        )
        ts = provider.get_table_schema(body.table_key)
        if ts is None:
            raise HTTPException(status_code=404, detail=f"Table '{body.table_key}' not found")
        return {
            "table_key": ts.full_name,
            "schema_name": ts.schema_name,
            "table_name": ts.table_name,
            "row_count_estimate": ts.row_count_estimate,
            "columns": [
                {
                    "name": c.name,
                    "data_type": c.data_type,
                    "nullable": c.nullable,
                    "primary_key": c.primary_key,
                    "foreign_key_ref": c.foreign_key_ref,
                }
                for c in ts.columns
            ],
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/sample/{table_key:path}")
def _legacy_get_sample_data_get(table_key: str) -> None:
    raise HTTPException(
        status_code=405,
        detail="Use POST /api/discovery/sample with a request body (connection_string must not be in URL query params).",
    )


@router.post("/sample")
def get_sample_data(body: DiscoverySampleRequest) -> dict[str, Any]:
    """
    Return PII-masked sample rows for a table.
    Data is masked BEFORE leaving this endpoint.
    File mode always returns empty rows (no live data).
    """
    try:
        from ama.mcp.factory import get_schema_provider

        provider = get_schema_provider(
            mode=body.mode,
            connection_string=body.connection_string,
            encrypted=body.encrypted,
        )
        cap = int(body.limit)
        if cap < 1:
            cap = 1
        if cap > 50:
            cap = 50
        rows = provider.get_sample_data(body.table_key, limit=cap)
        return {
            "table_key": body.table_key,
            "mode": body.mode,
            "limit": cap,
            "rows_returned": len(rows),
            "pii_masked": True,
            "rows": [r.data for r in rows],
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

