from __future__ import annotations
import logging
import json
from fastapi import APIRouter, Request
from starlette.responses import Response
from mcp.server import Server
from mcp.server.sse import SseServerTransport
import mcp.types as types
from ama.mcp.factory import get_schema_provider

logger = logging.getLogger("ama.mcp")
router = APIRouter(tags=["MCP"])

mcp_server = Server("AMA-Architect")

@mcp_server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="list_mssql_tables",
            description="List all tables in the connected SQL Server",
            inputSchema={
                "type": "object",
                "properties": {
                    "schema_filter": {"type": "string", "description": "Schema name (e.g. dbo)", "default": "dbo"}
                }
            }
        ),
        types.Tool(
            name="get_mssql_schema",
            description="Get column metadata for a specific SQL Server table",
            inputSchema={
                "type": "object",
                "properties": {
                    "table_name": {"type": "string", "description": "Name of the table"}
                },
                "required": ["table_name"]
            }
        )
    ]

@mcp_server.call_tool()
async def handle_call_tool(name: str, arguments: dict | None) -> list[types.TextContent]:
    try:
        provider = get_schema_provider(mode="sqlserver")
        
        if name == "list_mssql_tables":
            schema_filter = (arguments or {}).get("schema_filter", "dbo")
            tables = provider.list_tables(schema_filter=schema_filter)
            return [types.TextContent(type="text", text=json.dumps(tables))]
        
        elif name == "get_mssql_schema":
            table_name = (arguments or {}).get("table_name")
            if not table_name:
                return [types.TextContent(type="text", text="Error: table_name is required")]
            schema = provider.get_table_schema(table_key=table_name)
            if not schema:
                return [types.TextContent(type="text", text=f"Error: Table {table_name} not found")]
            
            data = {
                "table": schema.table_name,
                "columns": [{"name": c.name, "type": c.data_type} for c in schema.columns]
            }
            return [types.TextContent(type="text", text=json.dumps(data))]
            
    except Exception as e:
        logger.error(f"MCP Tool Execution Error: {e}")
        return [types.TextContent(type="text", text=f"Error: {str(e)}")]
    
    return [types.TextContent(type="text", text=f"Unknown tool: {name}")]

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