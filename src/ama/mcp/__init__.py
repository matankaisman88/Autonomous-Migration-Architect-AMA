"""
AMA MCP (Model Context Protocol) — Live DB schema provider package.

Supported modes:
  file       → FileSchemaProvider  (default; uses existing DDL JSON files)
  postgres   → PostgresSchemaProvider (psycopg2-binary)
  oracle     → OracleSchemaProvider   (python-oracledb)

Select mode via environment variable AMA_SCHEMA_MODE.
Connection string via AMA_DB_CONNECTION_STRING.
"""
from ama.mcp.factory import get_schema_provider

__all__ = ["get_schema_provider"]
