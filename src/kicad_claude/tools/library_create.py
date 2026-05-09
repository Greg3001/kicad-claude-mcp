"""Phase 13 — create custom symbols and footprints from MCP.

Tools:
    create_symbol      — emit a new (symbol ...) into <project>/lib/<lib>.kicad_sym
    create_footprint   — emit a new .kicad_mod into <project>/lib/<lib>.pretty/

Both register the lib in sym-lib-table / fp-lib-table so KiCAD picks it up
on next open. Run `index_libraries(force=True)` afterwards to make the new
lib_ids searchable via `search_symbol` / `search_footprint`.
"""

from __future__ import annotations

import logging

from kicad_claude import state
from kicad_claude.adapters import library_create as lc

logger = logging.getLogger("kicad-claude.tools.library_create")


def register(mcp) -> None:
    @mcp.tool()
    def create_symbol(
        lib_name: str,
        symbol_name: str,
        pins: list,
        body_width_mm: float = 5.08,
        body_height_mm: float = 5.08,
        reference_prefix: str = "U",
        value: str = "",
        footprint: str = "",
        datasheet: str = "~",
        description: str = "",
        keywords: str = "",
    ) -> dict:
        """Create a new schematic symbol in `<project>/lib/<lib_name>.kicad_sym`.

        Each pin is a dict with:
          - `number` (str, e.g. "1")
          - `name` (str, e.g. "VCC")
          - `x_mm`, `y_mm` (pin's electrical end position, in symbol-local coords)
          - `length_mm` (default 2.54)
          - `angle_deg` (0/90/180/270 — direction the pin POINTS away from the body)
          - `type` (input/output/passive/power_in/power_out/bidirectional/...)
          - `shape` (default "line")

        After running, call `index_libraries(force=True)` to make this
        symbol searchable via `search_symbol`.
        """
        proj = state.get_active()
        if not pins:
            raise ValueError("at least one pin required")
        return lc.create_symbol(
            proj.path,
            lib_name=lib_name, symbol_name=symbol_name,
            pins=list(pins),
            body_width_mm=body_width_mm, body_height_mm=body_height_mm,
            reference_prefix=reference_prefix,
            value=value, footprint=footprint, datasheet=datasheet,
            description=description, keywords=keywords,
        )

    @mcp.tool()
    def create_footprint(
        lib_name: str,
        footprint_name: str,
        pads: list,
        description: str = "",
        tags: str = "",
        add_courtyard: bool = True,
        add_silk_outline: bool = True,
    ) -> dict:
        """Create a new PCB footprint in `<project>/lib/<lib_name>.pretty/`.

        Each pad is a dict with:
          - `number` (str)
          - `type` ("smd" | "thru_hole" | "np_thru_hole")
          - `shape` ("rect" | "circle" | "oval" | "roundrect")
          - `x_mm`, `y_mm` (center position, footprint-local)
          - `size_x_mm`, `size_y_mm`
          - `drill_mm` (only for thru_hole; defaults to size − 0.4 mm for annular ring)
          - `layers` (optional override; sensible defaults for SMD vs THT)

        Auto-generates F.CrtYd courtyard + F.SilkS outline by default
        (toggle via flags). After running, call `index_libraries(force=True)`
        to make this footprint searchable via `search_footprint`.
        """
        proj = state.get_active()
        if not pads:
            raise ValueError("at least one pad required")
        return lc.create_footprint(
            proj.path,
            lib_name=lib_name, footprint_name=footprint_name,
            pads=list(pads),
            description=description, tags=tags,
            add_courtyard=add_courtyard,
            add_silk_outline=add_silk_outline,
        )
