"""KiCAD-Claude MCP server entry point.

Phases enabled here so far:
    0 — ping (health check)
    1 — project management (create_project, set_project, get_project_state,
        list_components)
    2 — library indexing (index_libraries, list_libraries, search_symbol,
        search_footprint, get_symbol_details)
    3 — schematic editing (add_symbol, remove_symbol, move_symbol, add_wire,
        add_label, add_power_symbol, add_no_connect, list_pins, get_pin_position)
    4 — sourcing (check_availability, find_or_fetch_symbol, import_vendor_zip,
        list_vendor_parts)
    5 — PCB editing (set_board_outline, list_footprints, add_footprint,
        move_footprint, place_footprints_grid, add_track, add_via)
    6 — autorouting (autoroute_pcb, export_dsn, import_ses) via Freerouting
    7 — validation (run_erc, run_drc) via kicad-cli + JSON parsing
    8 — hierarchical sheets + multi-layer PCBs (extra-spec)
    9 — manufacturing outputs (gerbers, drill, pos, BOM, netlist, render, SVG,
        export_fab_package)
   10 — design rules + net classes + fab presets + auto-annotation +
        update_pcb_from_schematic (closes the schematic↔PCB loop)
   11 — copper zones + mounting holes + silk + fiducials + sourcing-enriched BOM
   12 — diff pairs + length tuning (meander) + schematic buses
   13 — STEP 3D + custom DRC rules + multi-board + symbol/footprint editor
   14 — signal integrity (impedance) + thermal (IPC-2152) + RF (CPW, stitching) +
        EMC heuristics
   15 — panelization (rows × cols + mouse bites) + SPICE (ngspice wrapper) +
        thermal network + crosstalk + return-path continuity (closed-form FEM)

Future phases register additional tool groups via the same `register(mcp)`
pattern.
"""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

# Load .env early so adapters can read API credentials at import/use time.
load_dotenv(Path(__file__).parent / ".env")

from kicad_claude.tools.emc import register as register_emc_tools  # noqa: E402
from kicad_claude.tools.library import register as register_library_tools  # noqa: E402
from kicad_claude.tools.library_create import register as register_library_create_tools  # noqa: E402
from kicad_claude.tools.manufacturing import register as register_manufacturing_tools  # noqa: E402
from kicad_claude.tools.panelization import register as register_panelization_tools  # noqa: E402
from kicad_claude.tools.pcb import register as register_pcb_tools  # noqa: E402
from kicad_claude.tools.project import register as register_project_tools  # noqa: E402
from kicad_claude.tools.rf import register as register_rf_tools  # noqa: E402
from kicad_claude.tools.routing import register as register_routing_tools  # noqa: E402
from kicad_claude.tools.rules import register as register_rules_tools  # noqa: E402
from kicad_claude.tools.schematic import register as register_schematic_tools  # noqa: E402
from kicad_claude.tools.signal_integrity import register as register_signal_integrity_tools  # noqa: E402
from kicad_claude.tools.simulation import register as register_simulation_tools  # noqa: E402
from kicad_claude.tools.sourcing import register as register_sourcing_tools  # noqa: E402
from kicad_claude.tools.spice import register as register_spice_tools  # noqa: E402
from kicad_claude.tools.sync import register as register_sync_tools  # noqa: E402
from kicad_claude.tools.thermal import register as register_thermal_tools  # noqa: E402
from kicad_claude.tools.validation import register as register_validation_tools  # noqa: E402
from kicad_claude.utils.logging import setup_logging  # noqa: E402

logger = setup_logging()

mcp = FastMCP("kicad-claude")


@mcp.tool()
def ping() -> str:
    """Health check. Returns 'pong' if the server is reachable."""
    logger.info("ping called")
    return "pong"


register_project_tools(mcp)
register_library_tools(mcp)
register_schematic_tools(mcp)
register_sourcing_tools(mcp)
register_pcb_tools(mcp)
register_routing_tools(mcp)
register_validation_tools(mcp)
register_manufacturing_tools(mcp)
register_rules_tools(mcp)
register_sync_tools(mcp)
register_library_create_tools(mcp)
register_signal_integrity_tools(mcp)
register_thermal_tools(mcp)
register_rf_tools(mcp)
register_emc_tools(mcp)
register_panelization_tools(mcp)
register_spice_tools(mcp)
register_simulation_tools(mcp)


if __name__ == "__main__":
    logger.info("starting kicad-claude MCP server (stdio)")
    mcp.run()
