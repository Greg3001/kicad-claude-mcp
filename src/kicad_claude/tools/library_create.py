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
from kicad_claude.adapters import footprint_pads
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
        replace: bool = False,
    ) -> dict:
        """Create a new schematic symbol in `<project>/lib/<lib_name>.kicad_sym`.

        Each pin is a dict with:
          - `number` (str, e.g. "1"; unique across the whole symbol)
          - `name` (str, e.g. "VCC")
          - `type` (input/output/passive/power_in/power_out/bidirectional/...)
          - `shape` (default "line")
          - `unit` (int >= 1, default 1): which unit of a multi-unit symbol
            the pin belongs to. Use it to split a 480-pin connector into
            several readable units, one `add_symbol(..., unit=N)` each.
          - `side` ("left" | "right", optional): used by auto layout.
          - `x_mm`, `y_mm` (optional): the pin's connection point in
            symbol-local coords, Y up. Give them for EVERY pin of a unit or
            for NONE. With none, the pins are stacked automatically on the
            left/right of a body sized to fit the names (pitch 2.54 mm).
          - `length_mm` (default 2.54)
          - `angle_deg` (explicit layout only): direction from the pin's
            connection point INTO the body, as KiCad stores it - 0 for a pin
            on the left edge, 180 on the right, 270 on the top, 90 on the bottom.

        `body_width_mm` / `body_height_mm` size the body of explicit-layout
        units; auto-layout units size themselves. `replace=True` overwrites an
        existing symbol of the same name.

        The symbol is placeable right away with `add_symbol` (project
        libraries are picked up automatically); `index_libraries` is not needed.
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
            description=description, keywords=keywords, replace=replace,
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
        validate: bool = True,
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
        (toggle via flags). The file is checked with `kicad-cli fp upgrade` and
        removed if KiCad rejects it. It is usable at once (project libraries
        are picked up automatically). For dual-row and QFN packages use
        `create_footprint_dual` / `create_footprint_qfn`.
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
            validate=validate,
        )

    @mcp.tool()
    def create_footprint_dual(
        lib_name: str,
        footprint_name: str,
        pin_count: int,
        pitch_mm: float,
        row_span_mm: float,
        pad_length_mm: float,
        pad_width_mm: float,
        ep_width_mm: float | None = None,
        ep_height_mm: float | None = None,
        ep_number: str | None = None,
        description: str = "",
        tags: str = "",
    ) -> dict:
        """Create a dual-row SMD footprint (SOIC, TSSOP, HTSSOP, PowerPAD...).

        Pin 1 is top-left, numbering counter-clockwise: the left column runs
        top to bottom, the right column bottom to top. `row_span_mm` is the
        pad-centre to pad-centre distance between the rows; `pad_length_mm`
        runs across the rows, `pad_width_mm` along them. Give `ep_width_mm` and
        `ep_height_mm` for a centred exposed (thermal) pad, numbered
        `ep_number` (default: the next number after the last pin) - make the
        symbol's pin number agree.
        """
        pads = footprint_pads.dual_row_pads(
            pin_count, pitch_mm, row_span_mm, pad_length_mm, pad_width_mm,
            ep_width_mm, ep_height_mm, ep_number)
        return lc.create_footprint(
            state.get_active().path, lib_name=lib_name, footprint_name=footprint_name,
            pads=pads, description=description, tags=tags)

    @mcp.tool()
    def create_footprint_qfn(
        lib_name: str,
        footprint_name: str,
        pins_per_side: int,
        pitch_mm: float,
        span_mm: float,
        pad_length_mm: float,
        pad_width_mm: float,
        ep_width_mm: float | None = None,
        ep_height_mm: float | None = None,
        ep_number: str | None = None,
        pins_top_bottom: int | None = None,
        span_y_mm: float | None = None,
        end_pad_length_mm: float | None = None,
        description: str = "",
        tags: str = "",
    ) -> dict:
        """Create a four-sided SMD footprint (QFN, VQFN, WQFN, LGA), square or rectangular.

        Pin 1 is top-left on the left side; numbering is counter-clockwise:
        left side top to bottom, bottom left to right, right bottom to top,
        top right to left. `pins_per_side` is the pad count of the left and
        right sides; `pins_top_bottom` that of the top and bottom ends (default
        the same: a square part). `span_mm` is the pad-centre distance between
        the left and right columns, `span_y_mm` between the top and bottom rows
        (default `span_mm`). `pad_length_mm` runs away from the body;
        `end_pad_length_mm` overrides it on the ends. Give the exposed-pad size
        for a centred thermal pad, numbered `ep_number` (default: the next
        number after the last pin - make the symbol agree).
        """
        pads = footprint_pads.qfn_pads(
            pins_per_side, pitch_mm, span_mm, pad_length_mm, pad_width_mm,
            ep_width_mm, ep_height_mm, ep_number,
            pins_top_bottom, span_y_mm, end_pad_length_mm)
        return lc.create_footprint(
            state.get_active().path, lib_name=lib_name, footprint_name=footprint_name,
            pads=pads, description=description, tags=tags)
