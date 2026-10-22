"""Phase 3 — schematic editing tools.

Tools (all operate on the active project's `.kicad_sch`):
    add_symbol, remove_symbol, move_symbol, set_symbol_field
    add_wire, add_junction, add_label, add_no_connect, add_power_symbol
    list_pins, get_pin_position
    list_components_detailed (richer than Phase 1's list_components)

Coordinates: millimetres, Y axis pointing UP (see utils/geometry). Use
multiples of 1.27 mm (ideally 2.54) so pins and wires land on KiCAD's
connection grid; `run_erc` reports `endpoint_off_grid` otherwise.
Rotations: 0 / 90 / 180 / 270 only.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from kicad_claude import state
from kicad_claude.adapters import annotation
from kicad_claude.adapters import sch_editor as ed
from kicad_claude.adapters import sch_connect, sch_io, sch_items, sch_sheets, sheet_tree, symbol_lib
from kicad_claude.utils import geometry
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


def _sheet_list() -> list[sheet_tree.SheetRef]:
    proj = state.get_active()
    return sheet_tree.walk(proj.path, proj.sch_path)


def _instance_path() -> str:
    """Build the KiCAD instance path string for the currently active context.

    - Root sheet:   `/{root_uuid}`
    - Sub-sheet:    `/{root_uuid}/{sheet uuid}`, one uuid per nesting level
    """
    proj = state.get_active()
    root_uuid = _root_uuid(proj.sch_path)
    active_filename = state.get_active_sheet_filename()
    if active_filename is None:
        return f"/{root_uuid}"
    try:
        ref = sheet_tree.find(_sheet_list(), active_filename)
    except KeyError as e:
        raise RuntimeError(
            f"active sheet {active_filename!r} is not registered in the project. "
            f"Was it removed manually?"
        ) from e
    return sheet_tree.instance_path(root_uuid, ref)


def _sheet_block(key: str) -> tuple[sheet_tree.SheetRef, Path, list, list]:
    """(ref, parent file, parent tree, the sheet block) for a sheet named by `key`."""
    proj = state.get_active()
    ref = sheet_tree.find(_sheet_list(), key)
    parent_path = sheet_tree.parent_file(proj.path, proj.sch_path, ref)
    parent_tree = sch_io.parse_file(parent_path)
    node = ed.find_sheet_by_filename(parent_tree, ref.filename)
    if node is None:
        raise RuntimeError(f"sheet {ref.filename!r} vanished from {parent_path.name}")
    return ref, parent_path, parent_tree, node


def _resolve_lib_symbol(lib_id: str) -> tuple[Path, str, dict]:
    """Look up `lib_id` in the indexer and return (lib_file, symbol_name, meta)."""
    idx = lib_tools._ensure_index()
    meta = idx["symbols"].get(lib_id)
    if meta is None:
        raise KeyError(
            f"unknown lib_id {lib_id!r} — did you call index_libraries first?"
        )
    if meta.get("path") and Path(meta["path"]).is_file():  # project-local library
        return Path(meta["path"]), meta["name"], meta
    for d in idx.get("symbol_dirs", []):
        path = Path(d) / f"{meta['lib']}.kicad_sym"
        if path.is_file():
            return path, meta["name"], meta
    raise FileNotFoundError(
        f"source .kicad_sym for {lib_id!r} not found; index may be stale"
    )


def project_sheet_paths() -> list[Path]:
    """Root schematic plus every sheet file below it that exists, at any depth."""
    proj = state.get_active()
    paths = [proj.sch_path]
    for ref in _sheet_list():
        path = proj.path / ref.filename
        if path.is_file() and path not in paths:
            paths.append(path)
    return paths


def _project_references() -> list[str]:
    """Every reference designator used on any sheet of the active project."""
    refs: list[str] = []
    for path in project_sheet_paths():
        refs += ed.all_references(sch_io.parse_file(path))
    return refs


def _next_power_reference(refs: list[str], prefix: str = "#PWR") -> str:
    """Smallest unused `#PWR####` / `#FLG####` across the WHOLE project.

    KiCad treats a repeated virtual reference on two sheets as an annotation
    error, which makes `kicad-cli sch export netlist` print a warning and the
    PCB update unreliable, so uniqueness has to be project-wide.
    """
    used = set()
    for ref in refs:
        m = re.fullmatch(re.escape(prefix) + r"0*(\d+)", ref or "")
        if m:
            used.add(int(m.group(1)))
    n = 1
    while n in used:
        n += 1
    return f"{prefix}{n:04d}"


def place_power_symbol(
    net: str, x_mm: float, y_mm: float, rotation: float = 0, style: str | None = None
) -> dict:
    """Place a power symbol on the active sheet (shared by the tools that need one)."""
    idx = lib_tools._ensure_index()
    candidate = f"power:{net}"
    if candidate not in idx["symbols"]:
        candidate = f"power:{style or '+3V3'}"
        if candidate not in idx["symbols"]:
            available = sorted(
                k.split(":", 1)[1] for k in idx["symbols"] if k.startswith("power:")
            )
            raise KeyError(
                f"no power symbol {candidate!r} to draw {net!r} with; "
                f"available styles: {available[:20]}{'...' if len(available) > 20 else ''}"
            )
    tree, path = _load_active_schematic()
    ref = _next_power_reference(
        _project_references(), "#FLG" if net == "PWR_FLAG" else "#PWR"
    )
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
        rotation=rotation,
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
        "lib_id": candidate,
        "position_mm": [x_mm, y_mm],
        "warnings": geometry.grid_warning([(x_mm, y_mm)], "power symbol origins"),
        "backup": str(backup) if backup else None,
    }


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
        unit: int = 1,
        footprint: str | None = None,
        fields: dict[str, str] | None = None,
    ) -> dict:
        """Add a symbol from the indexed KiCAD libraries to the active schematic.

        Args:
            lib_id: e.g. "Device:R" or "RF_Module:ESP32-S3-WROOM-1"
            reference: schematic-unique reference designator (e.g. "R1", "U2")
            value: human-visible value ("10k", "100uF", ...)
            x_mm, y_mm: position, MCP coords (Y up)
            rotation: 0/90/180/270 degrees CCW
            unit: which unit of a multi-unit part to place. Call again with
                the same `reference` and the next `unit` to place the others
                (they inherit the first unit's fields; `value` is then ignored).
            footprint: overrides the library's default footprint ("Lib:Name").
            fields: extra fields set at place time, e.g.
                {"MPN": "XCK26-SFVC784", "Manufacturer": "AMD", "Rad Grade": "COTS"}
                (hidden; use set_symbol_field with visible=True to show one).

        Returns the placed symbol's identity, plus `warnings` when pins or the
        origin are off the 1.27 mm grid. Refuses if `reference` already
        exists (except for a different unit of the same part).
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
            footprint=footprint if footprint is not None else meta.get("default_footprint", ""),
            datasheet=meta.get("datasheet", "~"),
            description=meta.get("description", ""),
            unit=unit,
        )
        for placed in ed.find_symbols_by_reference(tree, reference):  # every unit
            for name, field_value in (fields or {}).items():
                ed.set_symbol_field(placed, name, str(field_value))
        pin_points = [
            tuple(p["position_mm"]) for p in ed.list_pins_for_symbol(tree, reference, unit)
        ]
        warnings = geometry.grid_warning([(x_mm, y_mm), *pin_points], "points of this symbol")
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
            "unit": unit,
            "units_in_symbol": symbol_lib.unit_numbers(sym_def),
            "pin_count": meta.get("pin_count", 0),
            "sheet": state.get_active_sheet_filename() or "root",
            "warnings": warnings,
            "backup": str(backup) if backup else None,
        }

    @mcp.tool()
    def set_sheet_paper(
        paper: str = "A3",
        width_mm: float | None = None,
        height_mm: float | None = None,
    ) -> dict:
        """Set the paper size of the ACTIVE sheet (A5..A0, USLetter, USLegal, USLedger, User).

        `paper="User"` takes a custom `width_mm` x `height_mm` (landscape).

        Call it on a fresh sheet, before placing anything: it refuses a sheet
        that already has content, because the Y-up coordinates are flipped
        around the page height and a later change would move every item. A
        4-unit, 480-pin SOM connector needs at least A3; the default A4
        silently clips pins that fall off the page.
        """
        tree, path = _load_active_schematic()
        sch_sheets.set_paper(tree, paper, width_mm, height_mm)
        backup = _save_with_backup(tree, path)
        width, height = sch_sheets.page_size_mm(tree)
        return {
            "paper": paper,
            "size_mm": [width, height],
            "sheet": state.get_active_sheet_filename() or "root",
            "backup": str(backup) if backup else None,
        }

    @mcp.tool()
    def remove_symbol(reference: str, unit: int | None = None) -> dict:
        """Remove a symbol from the active schematic (all its units, or only `unit`)."""
        tree, path = _load_active_schematic()
        if not ed.remove_symbol(tree, reference, unit):
            raise KeyError(f"no symbol with reference {reference!r}")
        backup = _save_with_backup(tree, path)
        return {"removed": reference, "unit": unit, "backup": str(backup) if backup else None}

    @mcp.tool()
    def move_symbol(
        reference: str,
        x_mm: float,
        y_mm: float,
        rotation: float | None = None,
        unit: int | None = None,
    ) -> dict:
        """Move (and optionally rotate) an existing symbol. Absolute positioning.

        Reference/Value move with the symbol; a rotation change re-places them.
        `unit` is required when the part has several units placed.
        """
        tree, path = _load_active_schematic()
        ed.move_symbol(tree, reference, x_mm, y_mm, rotation, unit)
        backup = _save_with_backup(tree, path)
        return {
            "reference": reference,
            "position_mm": [x_mm, y_mm],
            "rotation": rotation,
            "backup": str(backup) if backup else None,
        }

    @mcp.tool()
    def set_symbol_field(
        reference: str,
        field: str,
        value: str,
        visible: bool | None = None,
    ) -> dict:
        """Set a field of a placed symbol, e.g. its Footprint, Value or an MPN.

        Creates the field if the symbol does not have it yet (hidden unless
        `visible` is True). `visible=None` keeps an existing field's visibility.
        Renaming the Reference this way is refused if the new one is taken.
        """
        tree, path = _load_active_schematic()
        nodes = ed.find_symbols_by_reference(tree, reference)
        if not nodes:
            raise KeyError(f"no symbol with reference {reference!r}")
        if field == "Reference" and value != reference and not value.endswith("?"):
            if ed.find_symbol_by_reference(tree, value) is not None:
                raise ValueError(f"reference {value!r} already exists")
        created = False
        for s_node in nodes:  # every unit of a multi-unit part carries the field
            created = ed.set_symbol_field(s_node, field, value, visible) or created
            if field == "Reference":
                # KiCAD reads the reference from (instances ...), not just the property.
                annotation._update_instance_reference(s_node, value)
        backup = _save_with_backup(tree, path)
        return {
            "reference": value if field == "Reference" else reference,
            "field": field,
            "value": value,
            "created": created,
            "backup": str(backup) if backup else None,
        }

    @mcp.tool()
    def add_junction(x_mm: float, y_mm: float) -> dict:
        """Add a junction dot where three or more wires/pins meet (T or cross)."""
        tree, path = _load_active_schematic()
        ed.add_junction(tree, x_mm, y_mm)
        backup = _save_with_backup(tree, path)
        return {
            "position_mm": [x_mm, y_mm],
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
            "warnings": geometry.grid_warning([(x1_mm, y1_mm), (x2_mm, y2_mm)], "wire ends"),
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
            "warnings": geometry.grid_warning([(x_mm, y_mm)], "label anchors"),
            "orientation": orientation,
            "backup": str(backup) if backup else None,
        }

    @mcp.tool()
    def add_power_symbol(
        net: str,
        x_mm: float,
        y_mm: float,
        rotation: float = 0,
        style: str | None = None,
    ) -> dict:
        """Place a power symbol (e.g. +5V, +3V3, GND, PWR_FLAG) on the active sheet.

        Auto-assigns a hidden `#PWR####` (or `#FLG####`) reference, unique
        across all sheets. A power symbol's NET NAME is its Value text, so a
        rail the `power` library has no symbol for (VCC_1V8_PL, +0V85, ...) is
        drawn with a stock symbol's graphics (`style`) and the custom value.

        Args:
            net: net name, e.g. "GND", "+3V3", "VCC_PSINT_FP".
            rotation: 0/90/180/270 (a supply arrow points up at 0, GND down).
            style: library symbol to borrow the drawing from when `power:{net}`
                does not exist. Default `+3V3` (arrow up); use `GND` for a
                ground-type rail.
        """
        return place_power_symbol(net, x_mm, y_mm, rotation, style)

    @mcp.tool()
    def add_no_connect(reference: str, pin: str, unit: int | None = None) -> dict:
        """Mark a pin as no-connect by placing a NC marker at its position."""
        tree, path = _load_active_schematic()
        x, y = ed.get_pin_position(tree, reference, pin, unit)
        ed.add_no_connect(tree, x, y)
        backup = _save_with_backup(tree, path)
        return {
            "reference": reference,
            "pin": pin,
            "position_mm": [x, y],
            "backup": str(backup) if backup else None,
        }

    @mcp.tool()
    def add_global_label(
        net_name: str,
        x_mm: float,
        y_mm: float,
        shape: str = "bidirectional",
        orientation: str = "right",
    ) -> dict:
        """Add a GLOBAL net label: the same name on any sheet is the same net.

        A plain `add_label` is sheet-scoped (it becomes `/<sheet>/NAME`), so a
        net that must meet across sheets without a sheet pin needs this (or a
        hierarchical label + sheet pin). Power symbols are already global.

        `shape`: input | output | bidirectional | tri_state | passive
        """
        tree, path = _load_active_schematic()
        sch_connect.add_global_label(tree, net_name, x_mm, y_mm, shape, orientation)
        backup = _save_with_backup(tree, path)
        return {
            "net": net_name,
            "shape": shape,
            "position_mm": [x_mm, y_mm],
            "sheet": state.get_active_sheet_filename() or "root",
            "backup": str(backup) if backup else None,
        }

    @mcp.tool()
    def connect_pins(
        reference: str,
        connections: list[dict],
        unit: int | None = None,
        scope: str = "local",
        stub_mm: float = 5.08,
        shape: str = "bidirectional",
    ) -> dict:
        """Wire many pins of one symbol to nets in a single call.

        Each connection is `{"pin": "A12", "net": "DDR_DQ0"}` or
        `{"pin": "B3", "nc": true}`. For each net a short stub wire leaves the
        pin straight away from the body and ends in a label; for `nc` a
        no-connect marker is placed on the pin. Everything is validated first;
        on any error nothing is written.

        `scope` sets the label kind for all connections (override per item
        with its own `scope`): `local` (sheet-scoped), `global` (same net on
        every sheet), or `hierarchical` (exposes the net to the parent sheet;
        only valid on a sub-sheet). `shape` applies to global/hierarchical.
        `unit` restricts to one placed unit of a multi-unit part.
        """
        if scope == "hierarchical" or any(
            c.get("scope") == "hierarchical" for c in connections
        ):
            if state.get_active_sheet_filename() is None:
                raise RuntimeError("hierarchical labels belong on a sub-sheet")
        tree, path = _load_active_schematic()
        plan = sch_connect.plan_connections(
            tree, reference, connections, unit=unit,
            default_scope=scope, default_shape=shape,
        )
        counts = sch_connect.apply_plan(tree, plan, stub_mm)
        backup = _save_with_backup(tree, path)
        return {
            "reference": reference,
            **counts,
            "sheet": state.get_active_sheet_filename() or "root",
            "backup": str(backup) if backup else None,
        }

    @mcp.tool()
    def add_text_note(text: str, x_mm: float, y_mm: float, size_mm: float = 1.27) -> dict:
        """Add a free text note (newlines allowed) to the active sheet."""
        tree, path = _load_active_schematic()
        sch_items.add_text(tree, text, x_mm, y_mm, size_mm)
        backup = _save_with_backup(tree, path)
        return {"position_mm": [x_mm, y_mm], "chars": len(text),
                "backup": str(backup) if backup else None}

    @mcp.tool()
    def set_dnp(reference: str, dnp: bool = True) -> dict:
        """Mark a part Do-Not-Populate (all its units); `export_bom(exclude_dnp=True)` drops it."""
        tree, path = _load_active_schematic()
        units = sch_items.set_dnp(tree, reference, dnp)
        backup = _save_with_backup(tree, path)
        return {"reference": reference, "dnp": dnp, "units": units,
                "backup": str(backup) if backup else None}

    @mcp.tool()
    def add_items(items: list[dict]) -> dict:
        """Draw many items on the active sheet in ONE file write.

        Each item is a dict with `op` and its coordinates (MCP mm, Y up):
          wire {x1,y1,x2,y2} | junction {x,y} | no_connect {x,y} |
          label {net,x,y,orientation?} | global_label {net,x,y,shape?,orientation?} |
          hierarchical_label {net,x,y,shape?,orientation?} | text {text,x,y,size?}
        Validated as a whole: one bad item and nothing is written. Max 5000 items.
        Prefer `connect_pins` for pin-to-net wiring; use this for the rest
        (buses of wires, power rails, notes).
        """
        tree, path = _load_active_schematic()
        counts = sch_items.apply_items(tree, items)
        points = [
            (op[a], op[b]) for op in items for a, b in (("x", "y"), ("x1", "y1"), ("x2", "y2"))
            if a in op and b in op
        ]
        backup = _save_with_backup(tree, path)
        return {"applied": counts, "warnings": geometry.grid_warning(points, "item points"),
                "backup": str(backup) if backup else None}

    @mcp.tool()
    def list_pins(reference: str, unit: int | None = None) -> list[dict]:
        """List pins of a placed symbol with their absolute positions (MCP coords).

        For multi-unit parts every placed unit is listed (each pin has a
        `unit` field); pass `unit` to restrict to one.
        """
        tree, _ = _load_active_schematic()
        return ed.list_pins_for_symbol(tree, reference, unit)

    @mcp.tool()
    def get_pin_position(reference: str, pin: str, unit: int | None = None) -> dict:
        """Return absolute (x, y) of one pin in MCP coordinates."""
        tree, _ = _load_active_schematic()
        x, y = ed.get_pin_position(tree, reference, pin, unit)
        return {"reference": reference, "pin": pin, "position_mm": [x, y]}

    # ----- Buses (visual grouping of multiple nets) ----------------------- #

    @mcp.tool()
    def add_bus(
        x1_mm: float,
        y1_mm: float,
        x2_mm: float,
        y2_mm: float,
    ) -> dict:
        """Add a bus line to the active sheet.

        A bus is the thick line that visually groups several wires (e.g.
        an 8-bit data bus). Each wire that taps off the bus uses
        `add_bus_entry` and a `add_label` with the individual net name.

        Bus naming uses KiCAD conventions:
          - Bracket form: `DATA[0..7]` for 8 wires
          - Alias form: declare with `add_bus_alias("DATA", ["D0", "D1", ...])`
        """
        tree, path = _load_active_schematic()
        ed.add_bus_segment(tree, x1_mm, y1_mm, x2_mm, y2_mm)
        backup = _save_with_backup(tree, path)
        return {
            "from_mm": [x1_mm, y1_mm],
            "to_mm": [x2_mm, y2_mm],
            "sheet": state.get_active_sheet_filename() or "root",
            "backup": str(backup) if backup else None,
        }

    @mcp.tool()
    def add_bus_entry(
        x_mm: float,
        y_mm: float,
        direction: str = "right_down",
    ) -> dict:
        """Add a `(bus_entry ...)` — the diagonal connector from a bus to a wire.

        `direction` is one of: right_down, right_up, left_down, left_up.
        Place at the point where the bus meets the entry; KiCAD draws a
        diagonal line from there to the wire side.
        """
        tree, path = _load_active_schematic()
        ed.add_bus_entry(tree, x_mm, y_mm, direction=direction)
        backup = _save_with_backup(tree, path)
        return {
            "position_mm": [x_mm, y_mm],
            "direction": direction,
            "sheet": state.get_active_sheet_filename() or "root",
            "backup": str(backup) if backup else None,
        }

    @mcp.tool()
    def add_bus_alias(alias_name: str, members: list[str]) -> dict:
        """Declare a bus alias: short name → list of member net names.

        Example: `add_bus_alias("MEM", ["MA0","MA1","MA2","MA3","MA4","MA5","MA6","MA7"])`.
        After this, the bus can be labelled "MEM" and KiCAD expands it to MA0..MA7.
        """
        tree, path = _load_active_schematic()
        ed.add_bus_alias(tree, alias_name, members)
        backup = _save_with_backup(tree, path)
        return {
            "alias": alias_name,
            "members": members,
            "member_count": len(members),
            "sheet": state.get_active_sheet_filename() or "root",
            "backup": str(backup) if backup else None,
        }

    # ----- Hierarchical sheets ------------------------------------------- #

    @mcp.tool()
    def add_sheet(
        sheet_name: str,
        sheet_filename: str | None = None,
        x_mm: float = 50,
        y_mm: float = 50,
        width_mm: float = 30,
        height_mm: float = 20,
        paper: str = "A4",
        paper_width_mm: float | None = None,
        paper_height_mm: float | None = None,
        parent: str = "",
    ) -> dict:
        """Create a sub-sheet under the root, or under another sub-sheet.

        Adds a `(sheet ...)` placeholder to the parent schematic and creates a
        fresh blank child `.kicad_sch` file. The active sheet does not change;
        call `set_active_sheet` to switch into the new one.

        `parent` is the sheet that holds the block: its name, filename or
        `a/b` path; empty means the root. A file can be used by one sheet only.
        Coordinates are on the parent sheet's page.

        If `sheet_filename` is omitted, it defaults to a slugified
        `{sheet_name}.kicad_sch`. `paper` sets the child's paper size
        ("User" with paper_width_mm / paper_height_mm for a custom one).
        """
        proj = state.get_active()
        if sheet_filename is None:
            slug = "".join(c if c.isalnum() or c in "_-" else "_" for c in sheet_name).strip("_")
            sheet_filename = f"{slug.lower() or 'subsheet'}.kicad_sch"
        if not sheet_filename.endswith(".kicad_sch"):
            raise ValueError("sheet_filename must end with .kicad_sch")

        sheets = _sheet_list()
        if any(sh.filename == sheet_filename for sh in sheets):
            raise ValueError(f"sheet {sheet_filename!r} already registered")
        root_uuid = _root_uuid(proj.sch_path)
        if parent:
            parent_ref = sheet_tree.find(sheets, parent)
            parent_path = proj.path / parent_ref.filename
            parent_inst = sheet_tree.instance_path(root_uuid, parent_ref)
        else:
            parent_path, parent_inst = proj.sch_path, None
        tree = sch_io.parse_file(parent_path)
        ed.add_sheet_node(
            tree,
            sheet_name=sheet_name,
            sheet_filename=sheet_filename,
            x_mm=x_mm,
            y_mm=y_mm,
            width_mm=width_mm,
            height_mm=height_mm,
            project_name=proj.name,
            parent_instance_path=parent_inst,
            page_no=len(sheets) + 2,
        )
        # Child first: a failure here must not leave a dangling sheet in the parent.
        child_path = proj.path / sheet_filename
        write_blank_schematic(child_path, paper, paper_width_mm, paper_height_mm)
        backup = _save_with_backup(tree, parent_path)
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

        # Accept a filename, a sheet name, or an `a/b` path for nested sheets.
        sheet = sheet_tree.find(_sheet_list(), sheet).filename

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
        """List the root and every sheet below it (`children` = root level, `all` = every depth)."""
        proj = state.get_active()
        root = sch_io.parse_file(proj.sch_path)
        children = ed.list_sheets(root)
        everything = [
            {"name": sh.name, "path": sh.path, "filename": sh.filename,
             "parent": sh.parent or "root", "depth": len(sh.names)}
            for sh in _sheet_list()
        ]
        return {
            "root": {"name": "root", "path": str(proj.sch_path)},
            "children": children,
            "count": len(children),
            "all": everything,
            "total": len(everything),
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
    def sync_sheet_pins(sheet_name: str) -> dict:
        """Create the sheet pins for a child sheet from its hierarchical labels.

        Reads the child's `hierarchical_label`s and adds a matching pin on the
        sheet block on its parent sheet for each one that has none (inputs, bidirectional
        and passive on the left edge, outputs on the right; the block grows to
        fit). Returns every pin with its MCP position so the root can be wired,
        and lists `stale` pins whose label no longer exists in the child.
        """
        proj = state.get_active()
        ref, parent_path, parent_tree, node = _sheet_block(sheet_name)
        child = sch_io.parse_file(proj.path / ref.filename)
        page_h = ed.page_height_mm(parent_tree)
        report = sch_sheets.sync_sheet_pins(node, sch_sheets.child_interface(child), page_h)
        backup = _save_with_backup(parent_tree, parent_path)
        return {
            "sheet": sheet_name,
            **report,
            "pins": sch_sheets.sheet_pin_positions(node, page_h),
            "backup": str(backup) if backup else None,
        }

    @mcp.tool()
    def label_sheet_pins(
        sheet_name: str,
        scope: str = "global",
        stub_mm: float = 5.08,
    ) -> dict:
        """Put a stub wire and a net label on every pin of a sheet block (on its parent sheet).

        The label carries the pin's name. Use `scope="global"` (default) so the
        same name joins the nets of other sheets, or `local` to keep it on the
        root. Run `sync_sheet_pins` first.
        """
        if scope not in ("local", "global"):
            raise ValueError("scope must be 'local' or 'global'")
        _ref, parent_path, parent_tree, node = _sheet_block(sheet_name)
        pins = sch_sheets.sheet_pin_positions(node, ed.page_height_mm(parent_tree))
        if not pins:
            raise ValueError(f"sheet {sheet_name!r} has no pins; run sync_sheet_pins first")
        plan = [
            sch_connect.PlannedPin(
                p["name"], p["position_mm"][0], p["position_mm"][1],
                p["angle"] % 360, p["name"], scope, p["shape"],
            )
            for p in pins
        ]
        counts = sch_connect.apply_plan(parent_tree, plan, stub_mm)
        backup = _save_with_backup(parent_tree, parent_path)
        return {"sheet": sheet_name, **counts, "backup": str(backup) if backup else None}

    @mcp.tool()
    def add_sheet_pin(
        sheet_name: str,
        pin_name: str,
        shape: str,
        x_mm: float,
        y_mm: float,
        orientation: str = "right",
    ) -> dict:
        """Add a sheet pin to a sub-sheet's block on its parent sheet.

        The pin must match a `hierarchical_label` of the same name inside
        the child sheet — that's how KiCAD bridges the nets.

        `pin_name` is the net name to expose (e.g., "+5V").
        `shape`: input | output | bidirectional | tri_state | passive
        """
        _ref, parent_path, parent_tree, sheet_node = _sheet_block(sheet_name)
        ed.add_sheet_pin(
            sheet_node,
            pin_name=pin_name,
            shape=shape,
            x_mm=x_mm,
            y_mm=y_mm,
            page_h=ed.page_height_mm(parent_tree),
            orientation=orientation,
        )
        backup = _save_with_backup(parent_tree, parent_path)
        return {
            "sheet": sheet_name,
            "pin": pin_name,
            "shape": shape,
            "orientation": orientation,
            "position_mm": [x_mm, y_mm],
            "backup": str(backup) if backup else None,
        }
