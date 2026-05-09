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

import json

from kicad_claude import state
from kicad_claude.adapters import pcb_editor as ed
from kicad_claude.adapters import project_settings as ps
from kicad_claude.adapters import sch_editor, sch_io
from kicad_claude.templates.blank import write_blank_pcb
from kicad_claude.tools import library as lib_tools

logger = logging.getLogger("kicad-claude.tools.pcb")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _load_active_pcb() -> tuple[list, Path]:
    pcb_path = state.get_active_board_path()
    tree = sch_io.parse_file(pcb_path)
    return tree, pcb_path


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
    def add_zone(
        net_name: str,
        layer: str,
        polygon_mm: list,
        fill_clearance_mm: float = 0.5,
        min_thickness_mm: float = 0.25,
        name: str = "",
    ) -> dict:
        """Add a copper zone (pour) to `net_name` on `layer`.

        `polygon_mm` is a list of `[x_mm, y_mm]` points in MCP coords (Y up).
        At least 3 points required. Layer can be a single copper layer
        ("F.Cu", "B.Cu", "In1.Cu", …) or "*.Cu" for all signal layers.

        After adding zones, run `run_drc(refill_zones=True)` so KiCAD
        computes the actual filled regions before validation.
        """
        polygon = [(float(p[0]), float(p[1])) for p in polygon_mm]
        tree, path = _load_active_pcb()
        ed.add_zone(
            tree,
            net_name=net_name, layer=layer, polygon_mcp=polygon,
            fill_clearance_mm=fill_clearance_mm,
            min_thickness_mm=min_thickness_mm,
            name=name,
        )
        backup = _save_with_backup(tree, path)
        return {
            "net_name": net_name,
            "layer": layer,
            "vertices": len(polygon),
            "backup": str(backup) if backup else None,
        }

    @mcp.tool()
    def add_ground_plane(
        layer: str = "B.Cu",
        net_name: str = "GND",
        fill_clearance_mm: float = 0.5,
    ) -> dict:
        """Pour a ground plane covering the whole board on `layer`.

        Reads the board outline (must exist — call `set_board_outline` first),
        creates a zone polygon matching it, and assigns it to `net_name`.
        Defaults: B.Cu / GND (the most common pattern).
        """
        tree, path = _load_active_pcb()
        ed.add_ground_plane(
            tree, layer=layer, net_name=net_name,
            fill_clearance_mm=fill_clearance_mm,
        )
        backup = _save_with_backup(tree, path)
        return {
            "layer": layer,
            "net_name": net_name,
            "backup": str(backup) if backup else None,
        }

    @mcp.tool()
    def add_silk_text(
        text: str,
        x_mm: float,
        y_mm: float,
        layer: str = "F.SilkS",
        size_mm: float = 1.0,
        rotation: float = 0,
    ) -> dict:
        """Add silkscreen text to the PCB.

        `layer`: F.SilkS / B.SilkS (silkscreen) or F.Fab / B.Fab (fab notes).
        For text on copper, use F.Cu / B.Cu — handy for IDs etched into copper.
        """
        tree, path = _load_active_pcb()
        ed.add_silk_text(
            tree, text=text, x_mm=x_mm, y_mm=y_mm,
            layer=layer, size_mm=size_mm, rotation=rotation,
        )
        backup = _save_with_backup(tree, path)
        return {
            "text": text,
            "position_mm": [x_mm, y_mm],
            "layer": layer,
            "size_mm": size_mm,
            "rotation": rotation,
            "backup": str(backup) if backup else None,
        }

    @mcp.tool()
    def add_mounting_hole(
        x_mm: float,
        y_mm: float,
        diameter_mm: float = 3.2,
        plated: bool = False,
        reference: str | None = None,
    ) -> dict:
        """Add a mounting hole at (x_mm, y_mm) by drill diameter.

        Common sizes:
          - 2.2 mm → M2 screw
          - 2.7 mm → M2.5 screw
          - 3.2 mm → M3 screw
          - 4.3 mm → M4 screw
          - 5.3 mm → M5 screw

        `plated=True` picks a `_Pad` variant (annular ring around the hole,
        useful for grounding the chassis). `plated=False` picks the bare
        non-plated through-hole.
        """
        idx = lib_tools._ensure_index()
        diam_str = f"{diameter_mm:g}"  # 3.2 → "3.2", 3.0 → "3"
        prefix = f"MountingHole:MountingHole_{diam_str}mm"

        candidates = [k for k in idx["footprints"] if k.startswith(prefix)]
        if plated:
            preferred = [
                k for k in candidates
                if "_Pad" in k
                and "TopOnly" not in k
                and "TopBottom" not in k
                and "Via" not in k
            ]
        else:
            preferred = [k for k in candidates if "_Pad" not in k]
        chosen = preferred or candidates
        if not chosen:
            available = sorted({
                f.split(":", 1)[1].split("_M")[0]
                for f in idx["footprints"]
                if f.startswith("MountingHole:MountingHole_")
            })
            raise FileNotFoundError(
                f"no MountingHole footprint for diameter {diameter_mm} mm "
                f"(plated={plated}). Available diameters: {available}"
            )
        lib_id = min(chosen, key=len)  # simplest matching name

        if reference is None:
            tree, _ = _load_active_pcb()
            existing_h = [
                ref for ref in ed.all_footprint_references(tree)
                if ref and ref.upper().startswith("H")
                and ref[1:].isdigit()
            ]
            n = len(existing_h) + 1
            reference = f"H{n}"

        mod_path = _resolve_footprint(lib_id)
        fp_def = ed.fetch_footprint_def(mod_path)
        tree, path = _load_active_pcb()
        ed.add_footprint(
            tree,
            qualified_lib_id=lib_id,
            reference=reference,
            value=f"{diameter_mm}mm",
            x_mm=x_mm, y_mm=y_mm,
            rotation=0, layer="F.Cu",
            fp_def_node=fp_def,
        )
        backup = _save_with_backup(tree, path)
        return {
            "reference": reference,
            "lib_id": lib_id,
            "diameter_mm": diameter_mm,
            "plated": plated,
            "position_mm": [x_mm, y_mm],
            "backup": str(backup) if backup else None,
        }

    @mcp.tool()
    def add_fiducial(
        x_mm: float,
        y_mm: float,
        size: str = "1mm",
        layer: str = "F.Cu",
        reference: str | None = None,
    ) -> dict:
        """Add a fiducial marker (for pick-and-place camera registration).

        `size` ∈ {"0.5mm", "0.75mm", "1mm", "1.5mm"}. Standard PnP machines
        need 3 fiducials per side, ideally near board corners. Layer defines
        which side: F.Cu (top fiducial) or B.Cu (bottom fiducial).
        """
        if size not in {"0.5mm", "0.75mm", "1mm", "1.5mm"}:
            raise ValueError(f"size must be one of 0.5mm, 0.75mm, 1mm, 1.5mm (got {size!r})")
        lib_id = f"Fiducial:Fiducial_{size}_Mask{size.replace('mm', '')}mm"
        # Several fiducial naming conventions exist; try the simpler one first.
        idx = lib_tools._ensure_index()
        if lib_id not in idx["footprints"]:
            # Common alternative names
            for cand in (
                f"Fiducial:Fiducial_{size}_Mask{size.replace('mm','')}mm",
                f"Fiducial:Fiducial_{size}_Mask2mm",
                f"Fiducial:Fiducial_{size}_CopperTop",
            ):
                if cand in idx["footprints"]:
                    lib_id = cand
                    break
            else:
                raise FileNotFoundError(
                    f"no Fiducial footprint matches size {size!r} in the index. "
                    f"Try add_footprint with an explicit Fiducial:* lib_id."
                )

        if reference is None:
            tree, _ = _load_active_pcb()
            existing = [
                ref for ref in ed.all_footprint_references(tree)
                if ref and ref.startswith("FID")
            ]
            n = len(existing) + 1
            reference = f"FID{n}"

        mod_path = _resolve_footprint(lib_id)
        fp_def = ed.fetch_footprint_def(mod_path)
        tree, path = _load_active_pcb()
        ed.add_footprint(
            tree,
            qualified_lib_id=lib_id,
            reference=reference,
            value=size,
            x_mm=x_mm, y_mm=y_mm,
            rotation=0, layer=layer,
            fp_def_node=fp_def,
        )
        backup = _save_with_backup(tree, path)
        return {
            "reference": reference,
            "lib_id": lib_id,
            "size": size,
            "layer": layer,
            "position_mm": [x_mm, y_mm],
            "backup": str(backup) if backup else None,
        }

    @mcp.tool()
    def list_diff_pair_candidates() -> dict:
        """Auto-detect differential pairs by net name conventions.

        Recognizes `_P/_N`, `+/-`, and `DP/DM` (USB-style) suffixes. Only
        returns pairs where BOTH members exist on the PCB. Useful before
        autorouting — pairs detected here will be routed coupled by
        Freerouting if the assigned net class has diff_pair_width / gap.
        """
        tree, _ = _load_active_pcb()
        return {"pairs": ed.find_diff_pair_candidates(tree)}

    @mcp.tool()
    def list_nets() -> dict:
        """List every net declared at the PCB top level."""
        tree, _ = _load_active_pcb()
        return {"nets": ed.list_nets(tree)}

    @mcp.tool()
    def compute_trace_length(net_name: str) -> dict:
        """Total trace length on a net (mm), summed across all layers/segments."""
        tree, _ = _load_active_pcb()
        return ed.compute_trace_length(tree, net_name)

    @mcp.tool()
    def validate_diff_pair_length_match(
        positive_net: str,
        negative_net: str,
        tolerance_mm: float = 0.5,
    ) -> dict:
        """Check that two diff pair nets are within `tolerance_mm` of each other.

        Returns lengths, skew, and `within_tolerance` boolean. Use after
        autoroute to decide whether length tuning (add_meander) is needed.
        """
        tree, _ = _load_active_pcb()
        p = ed.compute_trace_length(tree, positive_net)
        n = ed.compute_trace_length(tree, negative_net)
        skew = abs(p["total_mm"] - n["total_mm"])
        return {
            "positive_net": positive_net,
            "negative_net": negative_net,
            "positive_length_mm": p["total_mm"],
            "negative_length_mm": n["total_mm"],
            "skew_mm": round(skew, 4),
            "tolerance_mm": tolerance_mm,
            "within_tolerance": skew <= tolerance_mm,
            "longer_net": positive_net if p["total_mm"] > n["total_mm"] else negative_net,
        }

    @mcp.tool()
    def add_meander(
        x1_mm: float,
        y1_mm: float,
        x2_mm: float,
        y2_mm: float,
        target_length_mm: float,
        amplitude_mm: float = 1.5,
        side: str = "up",
        width_mm: float = 0.25,
        layer: str = "F.Cu",
        net_name: str | None = None,
    ) -> dict:
        """Add a triangular-meander trace from (x1,y1) to (x2,y2) totaling `target_length_mm`.

        For length tuning of high-speed signals (DDR, USB, Ethernet diff
        pairs). The meander is placed perpendicular to the line connecting
        the endpoints; `side` chooses which side ("up" / "down" relative to
        the perpendicular direction).

        Typical workflow:
          1. autoroute_pcb → traces routed
          2. validate_diff_pair_length_match(p, n) → reports skew
          3. compute_trace_length(longer_net), compute_trace_length(shorter_net)
          4. Manually delete a straight segment of the shorter net in KiCAD
             GUI, then call add_meander between its endpoints with target =
             longer_length.

        Raises ValueError if `target_length_mm` is shorter than the straight
        distance, or the meander region doesn't fit (increase amplitude).
        """
        tree, path = _load_active_pcb()
        segs = ed.add_meander_segments(
            tree,
            start_mm=(x1_mm, y1_mm),
            end_mm=(x2_mm, y2_mm),
            target_length_mm=target_length_mm,
            amplitude_mm=amplitude_mm,
            side=side,
            width_mm=width_mm,
            layer=layer,
            net_name=net_name,
        )
        backup = _save_with_backup(tree, path)
        # Total achieved length
        total = 0.0
        import math
        for seg in segs:
            start = sch_io.find_child(seg, "start")
            end = sch_io.find_child(seg, "end")
            total += math.hypot(
                float(end[1]) - float(start[1]),
                float(end[2]) - float(start[2]),
            )
        return {
            "from_mm": [x1_mm, y1_mm],
            "to_mm": [x2_mm, y2_mm],
            "target_length_mm": target_length_mm,
            "achieved_length_mm": round(total, 4),
            "segment_count": len(segs),
            "amplitude_mm": amplitude_mm,
            "side": side,
            "layer": layer,
            "net": net_name or "(unconnected)",
            "backup": str(backup) if backup else None,
        }

    # ----- Multi-board management ---------------------------------------- #

    @mcp.tool()
    def add_board(name: str) -> dict:
        """Create an additional `.kicad_pcb` file in the project (multi-board).

        Useful for projects with several physical boards (main + breakout +
        debugger, for instance). The new board file is registered in the
        project's `boards` list and made the active board.

        Standard project flow continues to use the original `.kicad_pcb`
        unless you switch via `set_active_board`.
        """
        proj = state.get_active()
        if not name.endswith(".kicad_pcb"):
            filename = f"{name}.kicad_pcb"
        else:
            filename = name
        target = proj.path / filename
        if target.is_file():
            raise FileExistsError(f"{target} already exists")
        write_blank_pcb(target)

        # Register the board in the .kicad_pro `boards` array so KiCAD's
        # Project Manager shows it.
        pro = ps.load_pro(proj.pro_path)
        boards = pro.setdefault("boards", [])
        if filename not in boards:
            boards.append(filename)
        ps.save_pro(proj.pro_path, pro)

        state.set_active_board(filename)
        return {
            "filename": filename,
            "path": str(target),
            "active": True,
        }

    @mcp.tool()
    def list_boards() -> dict:
        """List every `.kicad_pcb` in the project directory + which is active."""
        proj = state.get_active()
        files = sorted(p.name for p in proj.path.glob("*.kicad_pcb"))
        active = state.get_active_board_filename()
        # Active resolves to the main board if not explicitly set
        main = proj.pcb_path.name
        return {
            "boards": files,
            "main_board": main,
            "active_board": active or main,
        }

    @mcp.tool()
    def set_active_board(filename: str = "") -> dict:
        """Switch the active PCB. Empty string returns to the project's main board.

        Subsequent PCB tools (`add_footprint`, `add_track`, `run_drc`, etc.)
        operate on the active board.
        """
        if not filename or filename.lower() == "main":
            state.set_active_board(None)
        else:
            if not filename.endswith(".kicad_pcb"):
                filename = f"{filename}.kicad_pcb"
            state.set_active_board(filename)
        active_path = state.get_active_board_path()
        return {
            "active_board": state.get_active_board_filename() or "main",
            "path": str(active_path),
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
