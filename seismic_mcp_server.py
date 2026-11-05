"""Seismic MCP Server — Streamable HTTP (ค่าเริ่มต้น) หรือ stdio

วิธีรัน:
    python seismic_mcp_server.py                       # Streamable HTTP ที่ 0.0.0.0:8000/mcp
    python seismic_mcp_server.py --transport stdio     # Claude Desktop (stdio)

Environment variables (โหมด HTTP):
    MCP_HOST (0.0.0.0)  MCP_PORT (8000)  MCP_PATH (/mcp)
    MCP_STATELESS (true)  MCP_JSON_RESPONSE (true)
    MCP_ALLOWED_HOSTS / MCP_ALLOWED_ORIGINS  คั่นด้วย comma; ถ้ากำหนดจะเปิดการป้องกัน DNS rebinding
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import os
import sys
from typing import Any

import jsonschema
import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.shared.exceptions import MCPError
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from seismic_core import TOOLS, ToolError

# log ออกที่ stderr เท่านั้น (ในโหมด stdio, stdout เป็นช่องทางของ JSON-RPC)
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    stream=sys.stderr,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("seismic-mcp")

SERVER_NAME = "seismic-mcp-server"
SERVER_VERSION = "0.1.0"


def _apply_defaults(schema: dict, args: dict) -> dict:
    out = dict(args)
    for key, prop in schema.get("properties", {}).items():
        if key not in out and "default" in prop:
            out[key] = copy.deepcopy(prop["default"])
    return out


def _validate(name: str, args: dict) -> dict:
    schema = TOOLS[name]["inputSchema"]
    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(args), key=lambda e: list(e.absolute_path))
    if errors:
        msgs = [f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}" for e in errors]
        raise MCPError(code=ToolError.INVALID_PARAMS,
                       message=f"Invalid arguments for '{name}': " + "; ".join(msgs),
                       data={"errors": msgs})
    return _apply_defaults(schema, args)


async def list_tools(ctx: Any, params: types.PaginatedRequestParams | None) -> types.ListToolsResult:
    return types.ListToolsResult(tools=[
        types.Tool(name=name, description=spec["description"], input_schema=spec["inputSchema"])
        for name, spec in TOOLS.items()
    ])


async def call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
    name = params.name
    if name not in TOOLS:
        raise MCPError(code=ToolError.INVALID_PARAMS, message=f"Unknown tool: {name}")
    args = _validate(name, params.arguments or {})
    log.info("tools/call %s %s", name, {k: v for k, v in args.items() if k not in ("events", "assets", "event")})
    try:
        result = await TOOLS[name]["handler"](args)
    except ToolError as e:
        log.warning("tool %s failed: %s", name, e.message)
        raise MCPError(code=e.code, message=e.message, data=e.data) from None
    return types.CallToolResult(
        content=[types.TextContent(text=json.dumps(result, ensure_ascii=False))],
        structured_content=result,
    )


server = Server(
    SERVER_NAME,
    version=SERVER_VERSION,
    instructions="Earthquake data (USGS, Thai Meteorological Department) and parametric "
                 "insurance analytics: match events to insured assets and compute payouts.",
    on_list_tools=list_tools,
    on_call_tool=call_tool,
)


def _env_bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _env_list(name: str) -> list[str]:
    return [s.strip() for s in os.environ.get(name, "").split(",") if s.strip()]


async def _health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "server": SERVER_NAME, "version": SERVER_VERSION})


def build_http_app(host: str, path: str):
    from mcp.server.transport_security import TransportSecuritySettings

    allowed_hosts, allowed_origins = _env_list("MCP_ALLOWED_HOSTS"), _env_list("MCP_ALLOWED_ORIGINS")
    security = None
    if allowed_hosts or allowed_origins:
        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed_hosts,
            allowed_origins=allowed_origins,
        )
    return server.streamable_http_app(
        streamable_http_path=path,
        stateless_http=_env_bool("MCP_STATELESS", True),
        json_response=_env_bool("MCP_JSON_RESPONSE", True),
        transport_security=security,
        host=host,
        custom_starlette_routes=[Route("/health", _health, methods=["GET"])],
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Seismic MCP server")
    parser.add_argument("--transport", choices=["streamable-http", "stdio"],
                        default=os.environ.get("MCP_TRANSPORT", "streamable-http"))
    parser.add_argument("--host", default=os.environ.get("MCP_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("MCP_PORT", "8000")))
    parser.add_argument("--path", default=os.environ.get("MCP_PATH", "/mcp"))
    a = parser.parse_args()

    if a.transport == "stdio":
        import anyio
        from mcp.server.stdio import stdio_server

        async def run_stdio() -> None:
            async with stdio_server() as (read, write):
                await server.run(read, write, server.create_initialization_options())

        log.info("Starting %s %s on stdio", SERVER_NAME, SERVER_VERSION)
        anyio.run(run_stdio)
    else:
        import uvicorn

        log.info("Starting %s %s on http://%s:%d%s", SERVER_NAME, SERVER_VERSION, a.host, a.port, a.path)
        uvicorn.run(build_http_app(a.host, a.path), host=a.host, port=a.port,
                    log_level=os.environ.get("LOG_LEVEL", "info").lower())


if __name__ == "__main__":
    main()
