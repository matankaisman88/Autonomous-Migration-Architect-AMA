"""MCP SSE tools for schema discovery across multiple provider modes.

Supported ``db_mode`` values are ``sqlserver``, ``postgres``, ``oracle``, and
``file``. For live database modes (``sqlserver``, ``postgres``, ``oracle``),
``AMA_DB_CONNECTION_STRING`` must be set in the environment.

Deprecated aliases ``list_mssql_tables`` and ``get_mssql_schema`` are kept for
backward compatibility; prefer ``list_tables`` and ``get_table_schema``.
"""

from __future__ import annotations
import logging
import json
from types import SimpleNamespace
from fastapi import APIRouter, Request
from starlette.responses import Response
from ama.mcp.factory import get_schema_provider

try:
    from mcp.server import Server
    from mcp.server.sse import SseServerTransport
    import mcp.types as types
except ModuleNotFoundError:  # pragma: no cover - local test fallback when mcp package is unavailable
    class _FallbackServer:
        def __init__(self, *_args, **_kwargs):
            pass

        def list_tools(self):
            def decorator(fn):
                return fn

            return decorator

        def call_tool(self):
            def decorator(fn):
                return fn

            return decorator

        async def run(self, *_args, **_kwargs):
            raise RuntimeError("mcp package is not installed")

        def create_initialization_options(self):
            return {}

    class _FallbackSseServerTransport:
        def __init__(self, *_args, **_kwargs):
            pass

        async def connect_sse(self, *_args, **_kwargs):
            raise RuntimeError("mcp package is not installed")

        async def handle_post_message(self, *_args, **_kwargs):
            raise RuntimeError("mcp package is not installed")

    class _FallbackTool:
        def __init__(self, name: str, description: str, inputSchema: dict):
            self.name = name
            self.description = description
            self.inputSchema = inputSchema

    class _FallbackTextContent:
        def __init__(self, type: str, text: str):
            self.type = type
            self.text = text

    Server = _FallbackServer
    SseServerTransport = _FallbackSseServerTransport
    types = SimpleNamespace(Tool=_FallbackTool, TextContent=_FallbackTextContent)

logger = logging.getLogger("ama.mcp")
router = APIRouter(tags=["MCP"])

mcp_server = Server("AMA-Architect")

@mcp_server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    db_mode_property = {
        "type": "string",
        "enum": ["sqlserver", "postgres", "oracle", "file"],
        "default": "sqlserver",
        "description": "Source dialect / provider mode",
    }
    return [
        types.Tool(
            name="list_tables",
            description="List tables from the selected schema provider",
            inputSchema={
                "type": "object",
                "properties": {
                    "db_mode": db_mode_property,
                    "schema_filter": {
                        "type": "string",
                        "description": "Schema or owner name filter (e.g. dbo, public)",
                    },
                },
            },
        ),
        types.Tool(
            name="get_table_schema",
            description="Get column metadata for a specific table key",
            inputSchema={
                "type": "object",
                "properties": {
                    "db_mode": db_mode_property,
                    "table_name": {
                        "type": "string",
                        "description": "Fully qualified table key (schema.table)",
                    },
                },
                "required": ["table_name"],
            },
        ),
        types.Tool(
            name="list_mssql_tables",
            description="Deprecated alias for list_tables (use list_tables instead)",
            inputSchema={
                "type": "object",
                "properties": {
                    "db_mode": db_mode_property,
                    "schema_filter": {
                        "type": "string",
                        "description": "Schema or owner name filter (e.g. dbo, public)",
                    },
                },
            },
        ),
        types.Tool(
            name="get_mssql_schema",
            description="Deprecated alias for get_table_schema (use get_table_schema instead)",
            inputSchema={
                "type": "object",
                "properties": {
                    "db_mode": db_mode_property,
                    "table_name": {
                        "type": "string",
                        "description": "Fully qualified table key (schema.table)",
                    },
                },
                "required": ["table_name"],
            },
        ),
    ]


def _dispatch_tool(name: str, arguments: dict | None) -> list[types.TextContent]:
    try:
        args = arguments or {}
        list_tools = {"list_tables", "list_mssql_tables"}
        schema_tools = {"get_table_schema", "get_mssql_schema"}

        if name not in list_tools and name not in schema_tools:
            return [types.TextContent(type="text", text=f"Unknown tool: {name}")]

        db_mode = args.get("db_mode", "sqlserver")
        provider = get_schema_provider(mode=db_mode)

        if name in list_tools:
            schema_filter = args.get("schema_filter")
            tables = provider.list_tables(schema_filter=schema_filter)
            return [types.TextContent(type="text", text=json.dumps(tables))]

        if name in schema_tools:
            table_name = args.get("table_name")
            if not table_name:
                return [types.TextContent(type="text", text="Error: table_name is required")]
            schema = provider.get_table_schema(table_key=table_name)
            if not schema:
                return [types.TextContent(type="text", text=f"Error: Table {table_name} not found")]

            data = {
                "table": schema.table_name,
                "columns": [{"name": c.name, "type": c.data_type} for c in schema.columns],
            }
            return [types.TextContent(type="text", text=json.dumps(data))]

    except Exception as e:
        logger.error(f"MCP Tool Execution Error: {e}")
        return [types.TextContent(type="text", text=f"Error: {str(e)}")]


@mcp_server.call_tool()
async def handle_call_tool(name: str, arguments: dict | None) -> list[types.TextContent]:
    return _dispatch_tool(name=name, arguments=arguments)

sse_transport = SseServerTransport("/mcp/messages")

class McpSseResponse(Response):
    async def __call__(self, scope, receive, send):
        async with sse_transport.connect_sse(scope, receive, send) as (read_stream, write_stream):
            await mcp_server.run(
                read_stream,
                write_stream,
                mcp_server.create_initialization_options()
            )

class McpMessageResponse(Response):
    async def __call__(self, scope, receive, send):
        await sse_transport.handle_post_message(scope, receive, send)

@router.get("/sse")
async def handle_sse_get(request: Request):
    return McpSseResponse(content=None)

@router.post("/messages")
async def handle_mcp_messages(request: Request):
    return McpMessageResponse(content=None)