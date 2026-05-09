"""Phase 3 — schematic editing tools.

Tools (all operate on the active project's `.kicad_sch`):
    add_symbol, remove_symbol, move_symbol
    add_wire, add_label, add_no_connect, add_power_symbol
    list_pins, get_pin_position
    list_components_detailed (richer than Phase 1's list_components)

Coordinates: millimetres, Y axis pointing UP (see utils/geometry).
Rotations: 0 / 90 / 180 / 270 only.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from kicad_claude import state
from kicad_claude.adapters import sch_editor as ed
from kicad_claude.adapters import sch_io
from kicad_claude.templates.blank import write_blank_schematic
from kicad_claude.tools import library as lib_tools

logger = logging.getLogger("kicad-claude.tools.schematic")


# --------------------------------------------------------------------------- #
# Helpers shared across tools
# --------------------------------------------------------------------------- #


def _load_active_schematic() -> tuple[list, Path]:
    """Return (tree, sch_path) for the currently active sheet (root by default)."""
    sch_path = state.get_active_sheet_path()
    tree = sch_io.parse_file(sch_path)
    return tree, sch_path


def _save_with_backup(tree: list, sch_path: Path) -> Path | None:
    backup = ed.backup_file(sch_path)
    sch_io.write_file(sch_path, tree)
    return backup


def _root_uuid(proj_sch_path: Path) -> str:
    root = sch_io.parse_file(proj_sch_path)
    u = sch_io.find_child(root, "uuid")
    if u and len(u) >= 2 and isinstance(u[1], str):
        return u[1]
    raise RuntimeError(f"root schematic {proj_sch_path} has no uuid")


def _instance_path() -> str:
    """Build the KiCAD instance path string for the currently active context.

    - Root sheet:  `/{root_uuid}`
    - Child sheet: `/{root_uuid}/{sheet_in_root_uuid}`
    """
    proj = state.get_active()
    root_uuid = _root_uuid(proj.sch_path)
    active_filename = state.get_active_sheet_filename()
    if active_filename is None:
        return f"/{root_uuid}"

    root_tree = sch_io.parse_file(proj.sch_path)
    sheet_node = ed.find_sheet_by_filename(root_tree, active_filename)
    if sheet_node is None:
        raise RuntimeError(
            f"active sheet {active_filename!r} is not registered in the root "
            f"schematic. Was it removed manually?"
        )
    return f"/{root_uuid}/{ed.get_sheet_uuid(sheet_node)}"


def _resolve_lib_symbol(lib_id: str) -> tuple[Path, str, dict]:
    """Look up `lib_id` in the indexer and return (lib_file, symbol_name, meta)."""
    idx = lib_tools._ensure_index()
    meta = idx["symbols"].get(lib_id)
    if meta is None:
        raise KeyError(
            f"unknown lib_id {lib_id!r} — did you call index_libraries first?"
        )
    for d in idx.get("symbol_dirs", []):
        path = Path(d) / f"{meta['lib']}.kicad_sym"
        if path.is_file():
            return path, meta["name"], meta
    raise FileNotFoundError(
        f"source .kicad_sym for {lib_id!r} not found; index may be stale"
    )


def _next_power_reference(tree: list) -> str:
    """Auto-increment a `#PWR####` reference, picking the smallest unused number."""
    used = set()
    for ref in ed.all_references(tree):
        if not ref:
            continue
        m = re.fullmatch(r"#PWR0*(\d+)", ref)
        if m:
            used.add(int(m.group(1)))
    n = 1
    while n in used:
        n += 1
    return f"#PWR{n:04d}"


# --------------------------------------------------------------------------- #
# Tool registration
# --------------------------------------------------------------------------- #


def register(mcp) -> None:
    """Register Phase 3 tools on the FastMCP instance."""

    @mcp.tool()
    def add_symbol(
        lib_id: str,
        reference: str,
        value: str,
        x_mm: float,
        y_mm: float,
        rotation: float = 0,
    ) -> dict:
        """Add a symbol from the indexed KiCAD libraries to the active schematic.

        Args:
            lib_id: e.g. "Device:R" or "RF_Module:ESP32-S3-WROOM-1"
            reference: schematic-unique reference designator (e.g. "R1", "U2")
            value: human-visible value ("10k", "100uF", ...)
            x_mm, y_mm: position, MCP coords (Y up)
            rotation: 0/90/180/270 degrees CCW

        Returns the placed symbol's identity. Refuses if `reference` already exists.
        """
        tree, path = _load_active_schematic()
        lib_path, sym_name, meta = _resolve_lib_symbol(lib_id)
        sym_def = ed.fetch_symbol_def(lib_path, sym_name)

        proj = state.get_active()
        ed.add_symbol(
            tree,
            qualified_lib_id=lib_id,
            reference=reference,
            value=value,
            x_mm=x_mm,
            y_mm=y_mm,
            rotation=rotation,
            sym_def_node=sym_def,
            project_name=proj.name,
            instance_path=_instance_path(),
            footprint=meta.get("default_footprint", ""),
            datasheet=meta.get("datasheet", "~"),
            description=meta.get("description", ""),
        )
        backup = _save_with_backup(tree, path)
        logger.info(
            "added %s (%s) on sheet=%s at (%s, %s)",
            reference, lib_id, state.get_active_sheet_filename() or "root", x_mm, y_mm
        )
        return {
            "reference": reference,
            "lib_id": lib_id,
            "value": value,
            "position_mm": [x_mm, y_mm],
            "rotation": rotation,
            "pin_count": meta.get("pin_count", 0),
            "sheet": state.get_active_sheet_filename() or "root",
            "backup": str(backup) if backup else None,
        }

    @mcp.tool()
    def remove_symbol(reference: str) -> dict:
        """Remove the symbol with the given reference from the active schematic."""
        tree, path = _load_active_schematic()
        if not ed.remove_symbol(tree, reference):
            raise KeyError(f"no symbol with reference {reference!r}")
        backup = _save_with_backup(tree, path)
        return {"removed": reference, "backup": str(backup) if backup else None}

    @mcp.tool()
    def move_symbol(
        reference: str,
        x_mm: float,
        y_mm: float,
        rotation: float | None = None,
    ) -> dict:
        """Move (and optionally rotate) an existing symbol. Absolute positioning."""
        tree, path = _load_active_schematic()
        ed.move_symbol(tree, reference, x_mm, y_mm, rotation)
        backup = _save_with_backup(tree, path)
        return {
            "reference": reference,
            "position_mm": [x_mm, y_mm],
            "rotation": rotation,
            "backup": str(backup) if backup else None,
        }

    @mcp.tool()
    def add_wire(x1_mm: float, y1_mm: float, x2_mm: float, y2_mm: float) -> dict:
        """Add a straight wire segment between two points (MCP coords)."""
        tree, path = _load_active_schematic()
        ed.add_wire(tree, x1_mm, y1_mm, x2_mm, y2_mm)
        backup = _save_with_backup(tree, path)
        return {
            "from_mm": [x1_mm, y1_mm],
            "to_mm": [x2_mm, y2_mm],
            "backup": str(backup) if backup else None,
        }

    @mcp.tool()
    def add_label(
        net_name: str,
        x_mm: float,
        y_mm: float,
        orientation: str = "right",
    ) -> dict:
        """Add a net label at a point. orientation ∈ {right, up, left, down}."""
        tree, path = _load_active_schematic()
        ed.add_label(tree, net_name, x_mm, y_mm, orientation)
        backup = _save_with_backup(tree, path)
        return {
            "net": net_name,
            "position_mm": [x_mm, y_mm],
            "orientation": orientation,
            "backup": str(backup) if backup else None,
        }

    @mcp.tool()
    def add_power_symbol(net: str, x_mm: float, y_mm: float) -> dict:
        """Place a power symbol (e.g. +5V, +3V3, GND) from the `power` library.

        Auto-assigns a `#PWR####` reference. The library symbol id is
        `power:{net}`; if that doesn't exist in the index, the call fails with
        a hint listing valid power nets.
        """
        candidate = f"power:{net}"
        idx = lib_tools._ensure_index()
        if candidate not in idx["symbols"]:
            available = sorted(
                k.split(":", 1)[1] for k in idx["symbols"] if k.startswith("power:")
            )
            raise KeyError(
                f"unknown power net {net!r} (looked up {candidate!r}); "
                f"available: {available[:20]}{'...' if len(available) > 20 else ''}"
            )

        tree, path = _load_active_schematic()
        ref = _next_power_reference(tree)
        lib_path, sym_name, meta = _resolve_lib_symbol(candidate)
        sym_def = ed.fetch_symbol_def(lib_path, sym_name)
        proj = state.get_active()
        ed.add_symbol(
            tree,
            qualified_lib_id=candidate,
            reference=ref,
            value=net,
            x_mm=x_mm,
            y_mm=y_mm,
            rotation=0,
            sym_def_node=sym_def,
            project_name=proj.name,
            instance_path=_instance_path(),
            footprint=meta.get("default_footprint", ""),
            datasheet=meta.get("datasheet", "~"),
            description=meta.get("description", ""),
        )
        backup = _save_with_backup(tree, path)
        return {
            "net": net,
            "reference": ref,
            "position_mm": [x_mm, y_mm],
            "backup": str(backup) if backup else None,
        }

    @mcp.tool()
    def add_no_connect(reference: str, pin: str) -> dict:
        """Mark a pin as no-connect by placing a NC marker at its position."""
        tree, path = _load_active_schematic()
        x, y = ed.get_pin_position(tree, reference, pin)
        ed.add_no_connect(tree, x, y)
        backup = _save_with_backup(tree, path)
        return {
            "reference": reference,
            "pin": pin,
            "position_mm": [x, y],
            "backup": str(backup) if backup else None,
        }

    @mcp.tool()
    def list_pins(reference: str) -> list[dict]:
        """List pins of a placed symbol with their absolute positions (MCP coords)."""
        tree, _ = _load_active_schematic()
        return ed.list_pins_for_symbol(tree, reference)

    @mcp.tool()
    def get_pin_position(reference: str, pin: str) -> dict:
        """Return absolute (x, y) of one pin in MCP coordinates."""
        tree, _ = _load_active_schematic()
        x, y = ed.get_pin_position(tree, reference, pin)
        return {"reference": reference, "pin": pin, "position_mm": [x, y]}

    # ----- Hierarchical sheets ------------------------------------------- #

    @mcp.tool()
    def add_sheet(
        sheet_name: str,
        sheet_filename: str | None = None,
        x_mm: float = 50,
        y_mm: float = 50,
        width_mm: float = 30,
        height_mm: float = 20,
    ) -> dict:
        """Create a child sub-sheet on the ROOT schematic.

        Adds a `(sheet ...)` placeholder to the root and creates a fresh
        blank child `.kicad_sch` file. The active context stays on root —
        call `set_active_sheet` to switch into the new child.

        If `sheet_filename` is omitted, it defaults to a slugified
        `{sheet_name}.kicad_sch`.
        """
        proj = state.get_active()
        if sheet_filename is None:
            slug = "".join(c if c.isalnum() or c in "_-" else "_" for c in sheet_name).strip("_")
            sheet_filename = f"{slug.lower() or 'subsheet'}.kicad_sch"
        if not sheet_filename.endswith(".kicad_sch"):
            raise ValueError("sheet_filename must end with .kicad_sch")

        # Always operate on root — sheets must live in the root.
        previous_sheet = state.get_active_sheet_filename()
        state.set_active_sheet(None)
        try:
            tree, root_path = _load_active_schematic()
            ed.add_sheet_node(
                tree,
                sheet_name=sheet_name,
                sheet_filename=sheet_filename,
                x_mm=x_mm,
                y_mm=y_mm,
                width_mm=width_mm,
                height_mm=height_mm,
                project_name=proj.name,
            )
            backup = _save_with_backup(tree, root_path)
            child_path = proj.path / sheet_filename
            write_blank_schematic(child_path)
        finally:
            if previous_sheet is not None:
                state.set_active_sheet(previous_sheet)
        logger.info("added sheet %s -> %s", sheet_name, sheet_filename)
        return {
            "sheet_name": sheet_name,
            "sheet_filename": sheet_filename,
            "child_path": str(child_path),
            "backup": str(backup) if backup else None,
        }

    @mcp.tool()
    def set_active_sheet(sheet: str = "") -> dict:
        """Switch the editing context to a sub-sheet (or back to root).

        `sheet` can be:
          - "" or "root" — operate on the root schematic
          - "<name>.kicad_sch" — operate on that child sheet
          - the sheet's name as registered via `add_sheet`
        """
        proj = state.get_active()
        if not sheet or sheet.lower() == "root":
            state.set_active_sheet(None)
            return {"active_sheet": "root", "path": str(proj.sch_path)}

        # Accept either filename or sheet_name
        if not sheet.endswith(".kicad_sch"):
            root_tree = sch_io.parse_file(proj.sch_path)
            node = ed.find_sheet_by_name(root_tree, sheet)
            if node is None:
                raise KeyError(f"no sheet named {sheet!r} in root")
            sheet = ed.get_sheet_filename(node) or ""
            if not sheet:
                raise RuntimeError(f"sheet {sheet!r} has no Sheetfile")

        state.set_active_sheet(sheet)
        return {
            "active_sheet": sheet,
            "path": str(state.get_active_sheet_path()),
        }

    @mcp.tool()
    def get_active_sheet() -> dict:
        """Return the currently active sub-sheet (or 'root')."""
        active = state.get_active_sheet_filename()
        return {
            "active_sheet": active or "root",
            "path": str(state.get_active_sheet_path()),
        }

    @mcp.tool()
    def list_sheets() -> dict:
        """List the root schematic and every registered child sheet."""
        proj = state.get_active()
        root = sch_io.parse_file(proj.sch_path)
        children = ed.list_sheets(root)
        return {
            "root": {"name": "root", "path": str(proj.sch_path)},
            "children": children,
            "count": len(children),
        }

    @mcp.tool()
    def add_hierarchical_label(
        net_name: str,
        x_mm: float,
        y_mm: float,
        shape: str = "input",
        orientation: str = "right",
    ) -> dict:
        """Add a hierarchical label on the active SUB-sheet.

        Hierarchical labels are how a child sheet exposes a net to the
        parent. Each label must be matched by a sheet pin of the same name
        in the parent's `(sheet ...)` block — see `add_sheet_pin`.

        `shape`: input | output | bidirectional | tri_state | passive
        `orientation`: right | up | left | down
        """
        if state.get_active_sheet_filename() is None:
            raise RuntimeError(
                "hierarchical labels belong on a sub-sheet; call set_active_sheet "
                "first or use add_label for the root."
            )
        tree, path = _load_active_schematic()
        ed.add_hierarchical_label(
            tree, net_name=net_name, x_mm=x_mm, y_mm=y_mm,
            shape=shape, orientation=orientation,
        )
        backup = _save_with_backup(tree, path)
        return {
            "net": net_name,
            "shape": shape,
            "orientation": orientation,
            "position_mm": [x_mm, y_mm],
            "sheet": state.get_active_sheet_filename(),
            "backup": str(backup) if backup else None,
        }

    @mcp.tool()
    def add_sheet_pin(
        sheet_name: str,
        pin_name: str,
        shape: str,
        x_mm: float,
        y_mm: float,
        orientation: str = "right",
    ) -> dict:
        """Add a sheet pin to a child sheet's placeholder on the ROOT.

        The pin must match a `hierarchical_label` of the same name inside
        the child sheet — that's how KiCAD bridges the nets.

        `pin_name` is the net name to expose (e.g., "+5V").
        `shape`: input | output | bidirectional | tri_state | passive
        """
        proj = state.get_active()
        previous = state.get_active_sheet_filename()
        state.set_active_sheet(None)
        try:
            tree, root_path = _load_active_schematic()
            sheet_node = ed.find_sheet_by_name(tree, sheet_name)
            if sheet_node is None:
                raise KeyError(f"no sheet named {sheet_name!r}")
            ed.add_sheet_pin(
                sheet_node,
                pin_name=pin_name,
                shape=shape,
                x_mm=x_mm,
                y_mm=y_mm,
                page_h=ed.page_height_mm(tree),
                orientation=orientation,
            )
            backup = _save_with_backup(tree, root_path)
        finally:
            if previous is not None:
                state.set_active_sheet(previous)
        return {
            "sheet": sheet_name,
            "pin": pin_name,
            "shape": shape,
            "orientation": orientation,
            "position_mm": [x_mm, y_mm],
            "backup": str(backup) if backup else None,
        }
