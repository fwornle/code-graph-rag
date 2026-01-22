#!/usr/bin/env python3
"""
Stdio proxy that connects to the SSE server

This is a lightweight proxy that Claude Code spawns (via stdio transport).
It connects to a shared SSE server, forwarding requests/responses.
This allows Claude to connect to containerized MCP servers.
"""

import asyncio
import json
import os
import sys
from typing import Any

from loguru import logger
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool


SSE_SERVER_URL = os.environ.get("CODE_GRAPH_RAG_SSE_URL", "http://localhost:3850")


def setup_logging() -> None:
    """Configure logging to stderr."""
    logger.remove()
    logger.add(
        sys.stderr,
        level="INFO",
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>",
    )


async def main() -> None:
    """Main entry point for the stdio proxy."""
    setup_logging()

    logger.info(f"[GraphCode Proxy] Connecting to SSE server at {SSE_SERVER_URL}")

    # Connect to the SSE server as a client
    try:
        async with sse_client(f"{SSE_SERVER_URL}/sse") as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as client:
                # Initialize the client session
                await client.initialize()
                logger.info(f"[GraphCode Proxy] Connected to SSE server at {SSE_SERVER_URL}")

                # Get the list of tools from the SSE server
                tools_result = await client.list_tools()
                cached_tools = tools_result.tools

                # Create the stdio server for Claude Code
                server = Server("graph-code")

                @server.list_tools()
                async def list_tools() -> list[Tool]:
                    """Forward list_tools to the SSE server."""
                    return cached_tools

                @server.call_tool()
                async def call_tool(name: str, arguments: dict) -> list[TextContent]:
                    """Forward tool calls to the SSE server."""
                    logger.info(f"[GraphCode Proxy] Forwarding tool call: {name}")

                    try:
                        result = await client.call_tool(name, arguments)
                        return result.content
                    except Exception as e:
                        error_msg = f"Error calling tool '{name}': {str(e)}"
                        logger.error(f"[GraphCode Proxy] {error_msg}")
                        return [TextContent(type="text", text=f"Error: {error_msg}")]

                # Run the stdio server
                logger.info("[GraphCode Proxy] Starting stdio server...")

                async with stdio_server() as (read, write):
                    await server.run(read, write, server.create_initialization_options())

    except ConnectionRefusedError:
        logger.error(f"[GraphCode Proxy] Failed to connect to SSE server at {SSE_SERVER_URL}")
        logger.error("[GraphCode Proxy] Make sure the code-graph-rag SSE server is running")
        sys.exit(1)
    except Exception as e:
        logger.error(f"[GraphCode Proxy] Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
