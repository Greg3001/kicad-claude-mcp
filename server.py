"""KiCAD-Claude MCP server entry point.

Phases enabled here so far:
    0 — ping (health check)
    1 — project management (create_project, set_project, get_project_state,
        list_components)
    2 — library indexing (index_libraries, list_libraries, search_symbol,
        search_footprint, get_symbol_details)

Future phases register additional tool groups via the same `register(mcp)`
pattern.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from kicad_claude.tools.library import register as register_library_tools
from kicad_claude.tools.project import register as register_project_tools
from kicad_claude.utils.logging import setup_logging

logger = setup_logging()

mcp = FastMCP("kicad-claude")


@mcp.tool()
def ping() -> str:
    """Health check. Returns 'pong' if the server is reachable."""
    logger.info("ping called")
    return "pong"


register_project_tools(mcp)
register_library_tools(mcp)


if __name__ == "__main__":
    logger.info("starting kicad-claude MCP server (stdio)")
    mcp.run()
