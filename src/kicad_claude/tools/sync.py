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
from kicad_claude.adapters import annotation, kicad_cli, kicad_python, pcb_editor, pcb_populate, sch_editor, sch_io
from kicad_claude.tools import pcb as pcb_tools
from kicad_claude.tools.schematic import project_sheet_paths

logger = logging.getLogger("kicad-claude.tools.sync")


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
        paths = project_sheet_paths()
        result = annotation.annotate_sheets(paths, sort_by_position=sort_by_position)
        return result

    @mcp.tool()
    def place_footprints_from_schematic(
        columns: int = 20,
        pitch_mm: float = 15.0,
        origin_x_mm: float = 10.0,
        origin_y_mm: float = 280.0,
        timeout_seconds: float = 90.0,
    ) -> dict:
        """Add every schematic part that is missing from the PCB, in one pass.

        Exports the schematic netlist, then places each part that has a
        Footprint field and is not on the board yet on a coarse grid
        (row-major in reference order, `columns` per row, `pitch_mm` apart,
        starting at the origin and going down). This is a staging area, not a
        placement: move parts afterwards. Parts without a footprint are listed
        in `no_footprint`; footprints that cannot be found (not in the global
        index or a project library) in `unresolved`, and do not stop the rest.

        Run `update_pcb_from_schematic` afterwards to assign nets to the pads.
        """
        proj = state.get_active()
        netlist_xml = proj.path / "fab" / f"{proj.name}-netlist.xml"
        netlist_xml.parent.mkdir(parents=True, exist_ok=True)
        kicad_cli.export_netlist(proj.sch_path, netlist_xml, fmt="kicadxml", timeout=timeout_seconds)
        parts = pcb_populate.netlist_parts(netlist_xml)
        pcb_path = state.get_active_board_path()
        tree = sch_io.parse_file(pcb_path)
        plan = pcb_populate.plan_placements(
            parts, set(pcb_editor.all_footprint_references(tree)), columns=columns,
            pitch_mm=pitch_mm, origin=(origin_x_mm, origin_y_mm),
        )
        result = pcb_populate.place_all(tree, plan, pcb_tools._resolve_footprint)
        backup = sch_editor.backup_file(pcb_path)
        sch_io.write_file(pcb_path, tree)
        return {
            "placed": len(result["placed"]),
            "unresolved": result["unresolved"],
            "no_footprint": plan["no_footprint"],
            "already_on_board": len(plan["already_on_board"]),
            "rows": plan["rows"],
            "pcb_path": str(pcb_path),
            "backup": str(backup) if backup else None,
        }

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
