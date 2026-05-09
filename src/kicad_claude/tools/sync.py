"""Phase 10 — schematic↔PCB sync tools.

Tools:
    annotate_schematic        — auto-number ?-suffixed references in all sheets
    update_pcb_from_schematic — propagate the schematic's netlist to the PCB
                                (assigns nets to footprint pads so Freerouting
                                actually has something to route)
"""

from __future__ import annotations

import logging
from pathlib import Path

from kicad_claude import state
from kicad_claude.adapters import annotation, kicad_cli, kicad_python
from kicad_claude.adapters import sch_io

logger = logging.getLogger("kicad-claude.tools.sync")


def _all_schematic_paths() -> list[Path]:
    """Return root + every registered child .kicad_sch in the active project."""
    proj = state.get_active()
    paths = [proj.sch_path]
    root = sch_io.parse_file(proj.sch_path)
    for entry in root[1:]:
        if not sch_io.is_call(entry, "sheet"):
            continue
        for prop in sch_io.find_children(entry, "property"):
            if len(prop) >= 3 and prop[1] == "Sheetfile" and isinstance(prop[2], str):
                child = proj.path / prop[2]
                if child.is_file():
                    paths.append(child)
                break
    return paths


def register(mcp) -> None:
    """Register Phase 10 sync tools."""

    @mcp.tool()
    def annotate_schematic(sort_by_position: bool = True) -> dict:
        """Auto-number every `?`-suffixed reference (R?, U?, C?, …) in the project.

        Walks the root and every hierarchical child sheet. Numbers continue
        consistently across sheets — R1 in root and R2 in a child won't
        collide. Existing numbered refs are preserved.

        Returns per-sheet stats and the total assignments made.
        """
        paths = _all_schematic_paths()
        result = annotation.annotate_sheets(paths, sort_by_position=sort_by_position)
        return result

    @mcp.tool()
    def update_pcb_from_schematic(
        timeout_seconds: float = 90.0,
    ) -> dict:
        """Propagate the schematic's netlist to the active PCB.

        Process:
        1. Export the schematic netlist as kicadxml.
        2. Use KiCAD's Python (`pcbnew`) to:
           - add missing nets to the PCB,
           - assign each pad's net based on the netlist.
        3. Save the PCB.

        Footprints in the schematic that aren't on the PCB are reported as
        `missing_in_pcb` — add them with `add_footprint` and re-run. This
        tool does NOT move/remove footprints; use `add_footprint`,
        `remove_footprint`, `move_footprint` for layout changes.

        After this, Freerouting can actually route the board (pads have
        net assignments).
        """
        proj = state.get_active()
        # 1) Export the netlist as kicadxml (different format than DSN)
        netlist_xml = proj.path / "fab" / f"{proj.name}-netlist.xml"
        netlist_xml.parent.mkdir(parents=True, exist_ok=True)
        kicad_cli.export_netlist(
            proj.sch_path, netlist_xml, fmt="kicadxml", timeout=timeout_seconds
        )

        # 2) Apply via pcbnew Python
        result = kicad_python.apply_netlist(
            state.get_active_board_path(), netlist_xml, timeout=timeout_seconds
        )

        # 3) Surface the artifacts and a hint if any refs are missing
        result["netlist_path"] = str(netlist_xml)
        result["pcb_path"] = str(state.get_active_board_path())
        if result.get("missing_in_pcb"):
            result["hint"] = (
                "Some schematic references are not on the PCB. Place them "
                "with add_footprint and re-run update_pcb_from_schematic."
            )
        return result
