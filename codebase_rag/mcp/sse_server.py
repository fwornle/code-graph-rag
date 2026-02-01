#!/usr/bin/env python3
"""
SSE-based MCP server for code-graph-rag

This server runs as a single persistent process that multiple Claude Code sessions
can connect to via HTTP/SSE transport. Designed for containerized deployments.
"""

import json
import os
import sys
import time
from typing import Any

from loguru import logger
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.types import TextContent, Tool
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route
import uvicorn

from codebase_rag.config import settings
from codebase_rag.mcp.tools import create_mcp_tools_registry
from codebase_rag.services.graph_service import MemgraphIngestor
from codebase_rag.services.llm import CypherGenerator


def setup_logging() -> None:
    """Configure logging to stderr for MCP stdio transport."""
    logger.remove()
    logger.add(
        sys.stderr,
        level="INFO",
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>",
    )


PORT = int(os.environ.get("CODE_GRAPH_RAG_PORT", "3850"))

# Global shared instances
ingestor: MemgraphIngestor | None = None
tools: Any = None

# Single shared transport for all sessions
# The transport internally manages session IDs and routing
sse_transport = SseServerTransport("/messages")

# Track active session count for health endpoint
active_sessions = 0

# Server startup time
server_start_time = time.time()


def get_project_root() -> str:
    """Get the project root from environment or settings."""
    from pathlib import Path

    repo_path = os.environ.get("TARGET_REPO_PATH") or settings.TARGET_REPO_PATH

    if not repo_path:
        repo_path = os.environ.get("CLAUDE_PROJECT_ROOT") or os.environ.get("PWD") or str(Path.cwd())

    return str(Path(repo_path).resolve())


def initialize_services() -> None:
    """Initialize shared services."""
    global ingestor, tools

    if ingestor is not None:
        return

    setup_logging()

    project_root = get_project_root()
    logger.info(f"[GraphCode SSE] Using project root: {project_root}")

    logger.info("[GraphCode SSE] Initializing services...")

    ingestor = MemgraphIngestor(
        host=settings.MEMGRAPH_HOST,
        port=settings.MEMGRAPH_PORT,
        batch_size=settings.MEMGRAPH_BATCH_SIZE,
    )

    cypher_generator = CypherGenerator()

    tools = create_mcp_tools_registry(
        project_root=project_root,
        ingestor=ingestor,
        cypher_gen=cypher_generator,
    )

    logger.info("[GraphCode SSE] Services initialized successfully")


def create_mcp_server() -> Server:
    """Create and configure an MCP server instance."""
    initialize_services()

    server = Server("graph-code")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        schemas = tools.get_tool_schemas()
        return [
            Tool(
                name=schema["name"],
                description=schema["description"],
                inputSchema=schema["inputSchema"],
            )
            for schema in schemas
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        logger.info(f"[GraphCode SSE] Calling tool: {name}")

        try:
            handler_info = tools.get_tool_handler(name)
            if not handler_info:
                error_msg = f"Unknown tool: {name}"
                logger.error(f"[GraphCode SSE] {error_msg}")
                return [TextContent(type="text", text=f"Error: {error_msg}")]

            handler, returns_json = handler_info
            result = await handler(**arguments)

            if returns_json:
                result_text = json.dumps(result, indent=2)
            else:
                result_text = str(result)

            return [TextContent(type="text", text=result_text)]

        except Exception as e:
            error_msg = f"Error executing tool '{name}': {str(e)}"
            logger.error(f"[GraphCode SSE] {error_msg}", exc_info=True)
            return [TextContent(type="text", text=f"Error: {error_msg}")]

    return server


async def health_endpoint(request: Request) -> JSONResponse:
    """Health check endpoint."""
    return JSONResponse({
        "status": "ok",
        "service": "code-graph-rag",
        "sessions": active_sessions,
        "initialized": ingestor is not None,
        "uptime": int(time.time() - server_start_time),
    })


async def sse_endpoint(request: Request) -> Response:
    """SSE endpoint for establishing the stream."""
    global active_sessions

    logger.info("New SSE connection request")

    # Create a new MCP server for this session
    server = create_mcp_server()

    active_sessions += 1
    logger.info(f"Active sessions: {active_sessions}")

    try:
        # The shared transport handles the SSE connection
        # It internally generates and manages the session ID
        async with sse_transport.connect_sse(
            request.scope,
            request.receive,
            request._send,
        ) as (read_stream, write_stream):
            logger.info("SSE session connected")
            await server.run(read_stream, write_stream, server.create_initialization_options())
    except Exception as e:
        logger.error(f"SSE session error: {e}", exc_info=True)
    finally:
        active_sessions -= 1
        logger.info(f"SSE session ended. Active sessions: {active_sessions}")

    # Return empty Response to avoid NoneType error when client disconnects
    return Response()


# Define routes
# Note: /messages uses Mount with the transport's handle_post_message ASGI app
routes = [
    Route("/health", health_endpoint, methods=["GET"]),
    Route("/sse", sse_endpoint, methods=["GET"]),
    Mount("/messages", app=sse_transport.handle_post_message),
]

app = Starlette(routes=routes)


def main() -> None:
    """Main entry point for the SSE server."""
    setup_logging()
    logger.info(f"[GraphCode SSE] Starting SSE server on port {PORT}...")

    # Pre-initialize services
    try:
        initialize_services()
    except Exception as e:
        logger.error(f"[GraphCode SSE] Failed to initialize services: {e}")
        # Continue anyway - services will be initialized on first request

    print(f"Code Graph RAG SSE Server listening on port {PORT}")
    print(f"Health check: http://localhost:{PORT}/health")
    print(f"SSE endpoint: http://localhost:{PORT}/sse")

    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")


if __name__ == "__main__":
    main()
