"""Phase 5 — PCB editing tools.

Tools (all operate on the active project's `.kicad_pcb`):
    set_board_outline
    list_footprints
    add_footprint                 (extends the spec — needed for headless flows;
                                   in the spec'd workflow, footprints arrive via
                                   "Update PCB from Schematic" in KiCAD GUI)
    move_footprint
    place_footprints_grid
    add_track
    add_via

Coordinates: millimetres, Y axis pointing UP (same as schematic tools).
Rotations: 0 / 90 / 180 / 270.
"""

from __future__ import annotations

import logging
from pathlib import Path

from kicad_claude import state
from kicad_claude.adapters import pcb_editor as ed
from kicad_claude.adapters import sch_editor, sch_io
from kicad_claude.tools import library as lib_tools

logger = logging.getLogger("kicad-claude.tools.pcb")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _load_active_pcb() -> tuple[list, Path]:
    proj = state.get_active()
    tree = sch_io.parse_file(proj.pcb_path)
    return tree, proj.pcb_path


def _save_with_backup(tree: list, pcb_path: Path) -> Path | None:
    backup = sch_editor.backup_file(pcb_path)
    sch_io.write_file(pcb_path, tree)
    return backup


def _resolve_footprint(lib_id: str) -> Path:
    """Find the source `.kicad_mod` for a given footprint lib_id via the index."""
    idx = lib_tools._ensure_index()
    fp_meta = idx["footprints"].get(lib_id)
    if fp_meta is None:
        raise KeyError(
            f"unknown footprint lib_id {lib_id!r} — call index_libraries first or check spelling"
        )
    if ":" not in lib_id:
        raise ValueError(f"lib_id must be 'Lib:Name' (got {lib_id!r})")
    lib_name, fp_name = lib_id.split(":", 1)

    # Search project libs first, then official.
    for d in idx.get("footprint_dirs", []):
        path = Path(d) / f"{lib_name}.pretty" / f"{fp_name}.kicad_mod"
        if path.is_file():
            return path
    raise FileNotFoundError(
        f"source .kicad_mod for {lib_id!r} not found; index may be stale"
    )


# --------------------------------------------------------------------------- #
# Tool registration
# --------------------------------------------------------------------------- #


def register(mcp) -> None:
    """Register Phase 5 tools on the FastMCP instance."""

    @mcp.tool()
    def set_layer_count(n: int) -> dict:
        """Reconfigure the active PCB to have `n` copper layers.

        `n` must be even, 2-32. The `(layers ...)` block and the
        `(setup (stackup ...) ...)` sub-block are regenerated to match.

        Call this BEFORE adding tracks/vias on inner layers — existing items
        on layers that no longer exist will trigger DRC errors.
        """
        tree, path = _load_active_pcb()
        result = ed.set_copper_layer_count(tree, n)
        backup = _save_with_backup(tree, path)
        result["backup"] = str(backup) if backup else None
        return result

    @mcp.tool()
    def set_board_outline(
        width_mm: float,
        height_mm: float,
        shape: str = "rect",
        origin_x_mm: float = 10.0,
        origin_y_mm: float = 10.0,
    ) -> dict:
        """Replace the Edge.Cuts outline with a width × height rectangle.

        The board's bottom-left corner sits at MCP (origin_x_mm, origin_y_mm).
        Shape currently supports 'rect' only; rounded corners come later.
        """
        tree, path = _load_active_pcb()
        result = ed.set_board_outline(
            tree, width_mm, height_mm, shape=shape,
            origin_x_mcp=origin_x_mm, origin_y_mcp=origin_y_mm,
        )
        backup = _save_with_backup(tree, path)
        result["backup"] = str(backup) if backup else None
        return result

    @mcp.tool()
    def list_footprints() -> list[dict]:
        """List all placed footprints with reference, value, position (MCP), layer."""
        tree, _ = _load_active_pcb()
        return ed.list_footprints_summary(tree)

    @mcp.tool()
    def add_footprint(
        lib_id: str,
        reference: str,
        value: str,
        x_mm: float,
        y_mm: float,
        rotation: float = 0,
        layer: str = "F.Cu",
    ) -> dict:
        """Place a footprint from the indexed KiCAD libraries.

        Outside the spec'd workflow (Update PCB from Schematic in KiCAD GUI),
        this is the way to populate a fresh PCB with footprints from this MCP.
        """
        tree, path = _load_active_pcb()
        mod_path = _resolve_footprint(lib_id)
        fp_def = ed.fetch_footprint_def(mod_path)
        ed.add_footprint(
            tree,
            qualified_lib_id=lib_id,
            reference=reference,
            value=value,
            x_mm=x_mm,
            y_mm=y_mm,
            rotation=rotation,
            layer=layer,
            fp_def_node=fp_def,
        )
        backup = _save_with_backup(tree, path)
        return {
            "reference": reference,
            "lib_id": lib_id,
            "value": value,
            "position_mm": [x_mm, y_mm],
            "rotation": rotation,
            "layer": layer,
            "backup": str(backup) if backup else None,
        }

    @mcp.tool()
    def move_footprint(
        reference: str,
        x_mm: float,
        y_mm: float,
        rotation: float | None = None,
        layer: str | None = None,
    ) -> dict:
        """Reposition (and optionally rotate / flip layer of) a placed footprint."""
        tree, path = _load_active_pcb()
        ed.move_footprint(tree, reference, x_mm, y_mm, rotation=rotation, layer=layer)
        backup = _save_with_backup(tree, path)
        return {
            "reference": reference,
            "position_mm": [x_mm, y_mm],
            "rotation": rotation,
            "layer": layer,
            "backup": str(backup) if backup else None,
        }

    @mcp.tool()
    def place_footprints_grid(
        spacing_mm: float = 10.0,
        columns: int = 5,
        origin_x_mm: float = 15.0,
        origin_y_mm: float = 15.0,
    ) -> dict:
        """Distribute footprints currently at (0,0) onto a regular grid.

        Useful right after KiCAD's Update PCB from Schematic, when every new
        footprint is stacked at the origin.
        """
        tree, path = _load_active_pcb()
        result = ed.place_footprints_grid(
            tree,
            spacing_mm=spacing_mm,
            columns=columns,
            origin_mcp=(origin_x_mm, origin_y_mm),
        )
        backup = _save_with_backup(tree, path)
        result["backup"] = str(backup) if backup else None
        return result

    @mcp.tool()
    def add_track(
        x1_mm: float,
        y1_mm: float,
        x2_mm: float,
        y2_mm: float,
        width_mm: float = 0.25,
        layer: str = "F.Cu",
        net: int = 0,
    ) -> dict:
        """Add a track segment between two points on the given copper layer."""
        tree, path = _load_active_pcb()
        ed.add_track(tree, x1_mm, y1_mm, x2_mm, y2_mm,
                     width_mm=width_mm, layer=layer, net=net)
        backup = _save_with_backup(tree, path)
        return {
            "from_mm": [x1_mm, y1_mm],
            "to_mm": [x2_mm, y2_mm],
            "width_mm": width_mm,
            "layer": layer,
            "net": net,
            "backup": str(backup) if backup else None,
        }

    @mcp.tool()
    def add_via(
        x_mm: float,
        y_mm: float,
        drill_mm: float = 0.4,
        diameter_mm: float = 0.8,
        net: int = 0,
    ) -> dict:
        """Add a through-hole via from F.Cu to B.Cu."""
        tree, path = _load_active_pcb()
        ed.add_via(tree, x_mm, y_mm, drill_mm=drill_mm, diameter_mm=diameter_mm, net=net)
        backup = _save_with_backup(tree, path)
        return {
            "position_mm": [x_mm, y_mm],
            "drill_mm": drill_mm,
            "diameter_mm": diameter_mm,
            "net": net,
            "backup": str(backup) if backup else None,
        }
