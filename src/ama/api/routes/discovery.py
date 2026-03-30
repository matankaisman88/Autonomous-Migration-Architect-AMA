"""
Discovery routes.

GET /discovery/tables     — list tables from live DB (or file manifest)
GET /discovery/schema     — get full column schema for one table
GET /discovery/sample     — get PII-masked sample rows for one table
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/discovery", tags=["Discovery"])


@router.get("/tables")
def list_tables(
    mode: str = Query(default="file", description="file | postgres | oracle"),
    connection_string: str | None = Query(default=None),
    schema_filter: str | None = Query(default=None, description="Filter by schema/owner name"),
) -> dict[str, Any]:
    """
    Pull table list directly from the selected DB (or file manifest).
    Replaces manual table name entry in the onboarding UI.
    """
    from ama.mcp.factory import get_schema_provider

    try:
        provider = get_schema_provider(mode=mode, connection_string=connection_string)
        tables = provider.get_table_list(schema_filter=schema_filter)
        return {
            "mode": mode,
            "schema_filter": schema_filter,
            "count": len(tables),
            "tables": tables,
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Discovery list_tables failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/schema/{table_key:path}")
def get_table_schema(
    table_key: str,
    mode: str = Query(default="file"),
    connection_string: str | None = Query(default=None),
) -> dict[str, Any]:
    """
    Return full column metadata for a single table.
    table_key format: schema.table  e.g. sales.orders
    """
    from ama.mcp.factory import get_schema_provider

    try:
        provider = get_schema_provider(mode=mode, connection_string=connection_string)
        ts = provider.get_table_schema(table_key)
        if ts is None:
            raise HTTPException(status_code=404, detail=f"Table '{table_key}' not found")
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
def get_sample_data(
    table_key: str,
    mode: str = Query(default="file"),
    connection_string: str | None = Query(default=None),
    limit: int = Query(default=5, ge=1, le=50),
) -> dict[str, Any]:
    """
    Return PII-masked sample rows for a table.
    Data is masked BEFORE leaving this endpoint.
    File mode always returns empty rows (no live data).
    """
    from ama.mcp.factory import get_schema_provider

    try:
        provider = get_schema_provider(mode=mode, connection_string=connection_string)
        rows = provider.get_sample_data(table_key, limit=limit)
        return {
            "table_key": table_key,
            "mode": mode,
            "limit": limit,
            "rows_returned": len(rows),
            "pii_masked": True,
            "rows": [r.data for r in rows],
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

