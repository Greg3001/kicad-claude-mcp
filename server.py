"""KiCAD-Claude MCP server entry point.

Phase 0 — Bootstrap. Only the `ping` tool is wired up. Real KiCAD tools land
in subsequent phases (see kicad-claude-mcp-spec.md §5).
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from src.kicad_claude.utils.logging import setup_logging

logger = setup_logging()

mcp = FastMCP("kicad-claude")


@mcp.tool()
def ping() -> str:
    """Health check. Returns 'pong' if the server is reachable."""
    logger.info("ping called")
    return "pong"


if __name__ == "__main__":
    logger.info("starting kicad-claude MCP server (stdio)")
    mcp.run()
