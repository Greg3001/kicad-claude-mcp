"""High-level mutations on a parsed `.kicad_sch` tree.

Operates on raw sexpdata trees — `parse_file` from `sch_io.py` returns the
top-level list, and these helpers mutate it in place. Use `write_file` to
serialize back.

Coordinate convention: all `x_mm`/`y_mm` parameters are in **MCP coordinates**
(Y up). Internal storage is in KiCAD coordinates (Y down). The `_mcp_to_at`
helper does the conversion at the boundary.
"""

from __future__ import annotations

import math
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from kicad_claude.adapters import sch_io, symbol_lib
from kicad_claude.adapters.sch_io import (
    find_child,
    find_children,
    head_of,
    is_call,
    sym,
)
from kicad_claude.utils.geometry import (
    DEFAULT_PAGE_HEIGHT_MM,
    SCH_GRID_MM,
    grid_aligned_height,
    mcp_to_kicad_xy,
    normalize_rotation,
    rotate_xy,
    round_mm,
)

# --------------------------------------------------------------------------- #
# Backup
# --------------------------------------------------------------------------- #


def backup_file(path: Path) -> Path | None:
    """Copy `path` to `<project>/.backups/<timestamp>_<filename>`. Idempotent if file missing."""
    path = Path(path)
    if not path.is_file():
        return None
    backups = path.parent / ".backups"
    backups.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = backups / f"{stamp}_{path.name}"
    shutil.copy2(path, dest)
    return dest


# --------------------------------------------------------------------------- #
# Page height (Y-flip parameter)
# --------------------------------------------------------------------------- #


def page_height_mm(tree: list) -> float:
    """Return the Y-flip height for this schematic (MCP y=0 maps to KiCAD y=H).

    This is the paper height (A4 landscape by default) rounded down to the
    2.54 mm grid, so MCP coordinates on the 1.27 mm grid stay on KiCAD's
    connection grid after the flip. For A4 that is 208.28 mm, not 210.
    """
    return grid_aligned_height(_paper_height_mm(tree))


def _paper_height_mm(tree: list) -> float:
    """Detect the schematic paper height. Defaults to A4 landscape (210mm)."""
    paper = find_child(tree, "paper")
    if paper and len(paper) >= 4 and paper[1] == "User":
        return float(paper[3])  # (paper "User" width height)
    if paper and len(paper) >= 2 and isinstance(paper[1], str):
        # KiCAD paper sizes (height in mm, landscape orientation by default):
        sizes = {
            "A0": 841.0,
            "A1": 594.0,
            "A2": 420.0,
            "A3": 297.0,
            "A4": 210.0,
            "A5": 148.0,
            "USLetter": 215.9,
            "USLegal": 215.9,
            "USLedger": 279.4,
        }
        return sizes.get(paper[1], DEFAULT_PAGE_HEIGHT_MM)
    return DEFAULT_PAGE_HEIGHT_MM


# --------------------------------------------------------------------------- #
# Symbol instance lookup
# --------------------------------------------------------------------------- #


def iter_instance_symbols(tree: list):
    """Yield top-level (symbol ...) nodes that are instances (skip lib_symbols container)."""
    for child in tree[1:]:
        if is_call(child, "symbol") and find_child(child, "lib_id"):
            yield child


def get_symbol_property(symbol_node: list, name: str) -> str | None:
    for prop in find_children(symbol_node, "property"):
        if len(prop) >= 3 and prop[1] == name and isinstance(prop[2], str):
            return prop[2]
    return None


def set_symbol_property(symbol_node: list, name: str, value: str) -> None:
    for prop in find_children(symbol_node, "property"):
        if len(prop) >= 3 and prop[1] == name:
            prop[2] = value
            return
    raise KeyError(f"property {name!r} not found on symbol")


def set_symbol_field(
    symbol_node: list,
    name: str,
    value: str,
    visible: bool | None = None,
) -> bool:
    """Set (or create) a symbol field. Returns True if the field was created.

    New fields are placed at the symbol origin and hidden unless `visible` is
    True. `visible=None` leaves an existing field's visibility untouched.
    """
    created = False
    prop = _find_property(symbol_node, name)
    if prop is None:
        at = find_child(symbol_node, "at")
        prop = _make_property(name, value, float(at[1]), float(at[2]), hide=not visible)
        # Keep properties grouped: insert after the last existing one.
        props = find_children(symbol_node, "property")
        idx = symbol_node.index(props[-1]) + 1 if props else len(symbol_node)
        symbol_node.insert(idx, prop)
        created = True
    else:
        prop[2] = value
    if visible is not None:
        _set_hidden(prop, not visible)
    return created


def symbol_unit(symbol_node: list) -> int:
    """The `(unit N)` of a placed symbol (1 when absent)."""
    u = find_child(symbol_node, "unit")
    return int(u[1]) if u is not None and len(u) >= 2 else 1


def find_symbols_by_reference(tree: list, reference: str) -> list[list]:
    """All placed nodes sharing `reference` (one per unit of a multi-unit part)."""
    return [
        s_node for s_node in iter_instance_symbols(tree)
        if get_symbol_property(s_node, "Reference") == reference
    ]


def find_symbol_by_reference(
    tree: list, reference: str, unit: int | None = None
) -> list | None:
    for s_node in find_symbols_by_reference(tree, reference):
        if unit is None or symbol_unit(s_node) == unit:
            return s_node
    return None


def all_references(tree: list) -> list[str]:
    return [get_symbol_property(s, "Reference") or "?" for s in iter_instance_symbols(tree)]


# --------------------------------------------------------------------------- #
# lib_symbols injection
# --------------------------------------------------------------------------- #


def get_or_create_lib_symbols(tree: list) -> list:
    block = find_child(tree, "lib_symbols")
    if block is not None:
        return block
    # Insert after (paper ...) for natural ordering.
    new = [sym("lib_symbols")]
    insert_at = 1
    for i, child in enumerate(tree[1:], start=1):
        if is_call(child, "paper"):
            insert_at = i + 1
            break
    tree.insert(insert_at, new)
    return new


def lib_symbols_has(tree: list, qualified_lib_id: str) -> bool:
    block = find_child(tree, "lib_symbols")
    if not block:
        return False
    for child in block[1:]:
        if is_call(child, "symbol") and len(child) >= 2 and child[1] == qualified_lib_id:
            return True
    return False


def inject_lib_symbol(tree: list, symbol_def_node: list) -> None:
    """Append a fully-qualified lib_symbols entry. Idempotent on lib_id."""
    block = get_or_create_lib_symbols(tree)
    qualified = symbol_def_node[1] if len(symbol_def_node) >= 2 else None
    if qualified and lib_symbols_has(tree, qualified):
        return
    block.append(symbol_def_node)


# --------------------------------------------------------------------------- #
# Symbol creation (from a lib def + placement parameters)
# --------------------------------------------------------------------------- #


def fetch_symbol_def(lib_path: Path, symbol_name: str) -> list:
    """Open a `.kicad_sym`, return a flat deep copy of the named (symbol ...) node.

    `extends` chains are resolved (see `symbol_lib`). The caller should set
    the node's name to "LibName:SymbolName" before injecting.
    """
    return symbol_lib.load_symbol_def(lib_path, symbol_name)


def make_lib_symbol_entry(symbol_def_node: list, qualified_lib_id: str) -> list:
    """Rename the symbol def's name to `Lib:Name` for lib_symbols use."""
    symbol_def_node[1] = qualified_lib_id
    return symbol_def_node


def collect_pin_numbers(symbol_def_node: list) -> list[str]:
    """Return pin number strings from a lib symbol definition."""
    pins: list[str] = []
    for child in symbol_def_node[2:]:
        h = head_of(child)
        if h == "pin":
            for sub in child[1:]:
                if is_call(sub, "number") and len(sub) >= 2 and isinstance(sub[1], str):
                    pins.append(sub[1])
        elif h == "symbol":
            pins.extend(collect_pin_numbers(child))
    return pins


def build_symbol_instance(
    qualified_lib_id: str,
    reference: str,
    value: str,
    x_mcp: float,
    y_mcp: float,
    rotation_deg: int,
    pin_numbers: list[str],
    project_name: str,
    instance_path: str,
    page_h: float,
    footprint: str = "",
    datasheet: str = "~",
    description: str = "",
    hide_reference: bool = False,
    unit: int = 1,
) -> list:
    """Construct a new (symbol ...) instance node ready to inject into the schematic."""
    x_k, y_k = mcp_to_kicad_xy(x_mcp, y_mcp, page_h)
    x_k, y_k = round_mm(x_k), round_mm(y_k)

    inst_uuid = str(uuid.uuid4())

    # Properties start at the symbol origin; `add_symbol` then runs
    # `autoplace_fields` to move Reference/Value clear of the body. We hide
    # Footprint/Datasheet/Description since they are mostly metadata.
    def _prop(name: str, val: str, hide: bool) -> list:
        return _make_property(name, val, x_k, y_k, hide=hide)

    pin_nodes = [
        [sym("pin"), num, [sym("uuid"), str(uuid.uuid4())]]
        for num in pin_numbers
    ]

    instances_node = [
        sym("instances"),
        [
            sym("project"),
            project_name,
            [
                sym("path"),
                instance_path,
                [sym("reference"), reference],
                [sym("unit"), unit],
            ],
        ],
    ]

    return [
        sym("symbol"),
        [sym("lib_id"), qualified_lib_id],
        [sym("at"), x_k, y_k, rotation_deg],
        [sym("unit"), unit],
        [sym("exclude_from_sim"), sym("no")],
        [sym("in_bom"), sym("yes")],
        [sym("on_board"), sym("yes")],
        [sym("dnp"), sym("no")],
        [sym("uuid"), inst_uuid],
        _prop("Reference", reference, hide=hide_reference),
        _prop("Value", value, hide=False),
        _prop("Footprint", footprint, hide=True),
        _prop("Datasheet", datasheet, hide=True),
        _prop("Description", description, hide=True),
        *pin_nodes,
        instances_node,
    ]


# --------------------------------------------------------------------------- #
# Public mutations
# --------------------------------------------------------------------------- #


_REFERENCE_RE = re.compile(r"^[A-Za-z_#]+[0-9]+$")


def _check_reference_format(reference: str) -> None:
    """Reject references KiCad cannot annotate: letters then digits, or `?`-suffixed.

    `U_LATCH` loads and passes ERC, but `kicad-cli sch export netlist` then
    reports annotation errors for the part; `U_LATCH1` is fine.
    """
    if reference.endswith("?") or _REFERENCE_RE.match(reference):
        return
    raise ValueError(
        f"reference {reference!r} has no trailing number, so KiCad reports an "
        f"annotation error on netlist export; use e.g. {reference.rstrip('0123456789')}1 "
        f"or {reference.rstrip('0123456789')}? to annotate later"
    )


def _check_unit_placement(
    tree: list, reference: str, qualified_lib_id: str, unit: int, units: list[int]
) -> list[list]:
    """Validate a (reference, unit) placement; return the already-placed siblings."""
    if unit not in units:
        raise ValueError(f"unit {unit} not in symbol {qualified_lib_id!r} (units: {units})")
    siblings = [] if reference.endswith("?") else find_symbols_by_reference(tree, reference)
    for node in siblings:
        lib_id = find_child(node, "lib_id")
        if lib_id is None or lib_id[1] != qualified_lib_id:
            raise ValueError(f"reference {reference!r} already exists")
        if symbol_unit(node) == unit:
            raise ValueError(f"reference {reference!r} unit {unit} already exists")
    return siblings


def _copy_fields_from(instance: list, sibling: list) -> None:
    """Make every unit of a part carry the same field values (Value, MPN, ...)."""
    for prop in find_children(sibling, "property"):
        if len(prop) >= 3 and isinstance(prop[2], str):
            set_symbol_field(instance, prop[1], prop[2])


def add_symbol(
    tree: list,
    *,
    qualified_lib_id: str,
    reference: str,
    value: str,
    x_mm: float,
    y_mm: float,
    rotation: float,
    sym_def_node: list,
    project_name: str,
    instance_path: str | None = None,
    footprint: str = "",
    datasheet: str = "~",
    description: str = "",
    unit: int = 1,
) -> list:
    """Inject a symbol into the schematic. Returns the new (symbol ...) node.

    `instance_path` is the KiCAD instance path (e.g. `/<root_uuid>` for the
    root sheet, `/<root_uuid>/<sheet_uuid>` for a child). If None, defaults
    to `/<this_file's_uuid>` — only correct for non-hierarchical use.

    Unannotated references (ending in `?`, e.g. `R?`) are explicitly allowed
    to repeat — they're meant to be resolved later by `annotate_schematic`.

    Multi-unit parts: call once per `unit` with the SAME reference. The
    instance lists only that unit's pins (plus any shared unit-0 pins), and
    later units inherit the fields of the first one placed.
    """
    _check_reference_format(reference)
    lib_entry_def = make_lib_symbol_entry(sym_def_node, qualified_lib_id)
    siblings = _check_unit_placement(
        tree, reference, qualified_lib_id, unit, symbol_lib.unit_numbers(lib_entry_def)
    )

    rot = normalize_rotation(rotation)
    page_h = page_height_mm(tree)
    if instance_path is None:
        instance_path = f"/{_schematic_uuid(tree)}"

    # Inject the lib symbol definition (idempotent on qualified id).
    inject_lib_symbol(tree, lib_entry_def)

    pins = unit_pin_numbers(lib_entry_def, unit)
    instance = build_symbol_instance(
        qualified_lib_id=qualified_lib_id,
        reference=reference,
        value=value,
        x_mcp=x_mm,
        y_mcp=y_mm,
        rotation_deg=rot,
        pin_numbers=pins,
        project_name=project_name,
        instance_path=instance_path,
        page_h=page_h,
        footprint=footprint,
        datasheet=datasheet,
        description=description,
        # Power symbols / virtual parts (#PWR, #FLG) keep their ref hidden, like KiCAD.
        hide_reference=is_power_symbol(lib_entry_def) or reference.startswith("#"),
        unit=unit,
    )
    if siblings:
        _copy_fields_from(instance, siblings[0])
    autoplace_fields(instance, lib_entry_def)
    tree.append(instance)
    return instance


def unit_pin_numbers(symbol_def_node: list, unit: int) -> list[str]:
    """Pin numbers an instance of `unit` carries, without duplicates."""
    seen: dict[str, None] = {}
    for pin_node, _u in symbol_lib.pins_of_unit(symbol_def_node, unit):
        seen.setdefault(_pin_id(pin_node)[0], None)
    return list(seen)


def remove_symbol(tree: list, reference: str, unit: int | None = None) -> bool:
    """Remove `reference` (every placed unit, or only `unit`). True if any removed."""
    doomed = [
        n for n in find_symbols_by_reference(tree, reference)
        if unit is None or symbol_unit(n) == unit
    ]
    for node in doomed:
        tree[:] = [c for c in tree if c is not node]
    return bool(doomed)


def move_symbol(
    tree: list,
    reference: str,
    x_mm: float,
    y_mm: float,
    rotation: float | None = None,
    unit: int | None = None,
) -> None:
    """Set absolute position (and optionally rotation) of an existing symbol.

    A multi-unit part has one node per unit, so `unit` is required for those.
    """
    placed = find_symbols_by_reference(tree, reference)
    if len(placed) > 1 and unit is None:
        raise ValueError(
            f"{reference!r} has {len(placed)} placed units; pass unit= to pick one"
        )
    s_node = find_symbol_by_reference(tree, reference, unit)
    if s_node is None:
        raise KeyError(f"no symbol with reference {reference!r}")
    page_h = page_height_mm(tree)
    x_k, y_k = mcp_to_kicad_xy(x_mm, y_mm, page_h)
    at = find_child(s_node, "at")
    if at is None or len(at) < 4:
        raise ValueError("symbol has malformed (at ...) node")
    dx, dy = round_mm(x_k) - float(at[1]), round_mm(y_k) - float(at[2])
    old_rot = int(at[3]) % 360
    at[1] = round_mm(x_k)
    at[2] = round_mm(y_k)
    # Fields are stored in absolute coords: translate them with the symbol.
    for prop in find_children(s_node, "property"):
        p_at = find_child(prop, "at")
        if p_at is not None and len(p_at) >= 3:
            p_at[1] = round_mm(float(p_at[1]) + dx)
            p_at[2] = round_mm(float(p_at[2]) + dy)
    if rotation is not None:
        at[3] = normalize_rotation(rotation)
        if at[3] != old_rot:
            lib_id = find_child(s_node, "lib_id")
            sym_def = find_lib_symbol_def(tree, lib_id[1]) if lib_id else None
            if sym_def is not None:
                autoplace_fields(s_node, sym_def)


def add_junction(tree: list, x_mm: float, y_mm: float) -> list:
    """Append a (junction ...) dot at a point (needed where 3+ wires meet)."""
    page_h = page_height_mm(tree)
    xk, yk = mcp_to_kicad_xy(x_mm, y_mm, page_h)
    node = [
        sym("junction"),
        [sym("at"), round_mm(xk), round_mm(yk)],
        [sym("diameter"), 0],
        [sym("color"), 0, 0, 0, 0],
        [sym("uuid"), str(uuid.uuid4())],
    ]
    tree.append(node)
    return node


def add_wire(
    tree: list,
    x1_mm: float,
    y1_mm: float,
    x2_mm: float,
    y2_mm: float,
) -> list:
    """Append a (wire ...) segment between two MCP-coord points. Returns the new node."""
    page_h = page_height_mm(tree)
    x1k, y1k = mcp_to_kicad_xy(x1_mm, y1_mm, page_h)
    x2k, y2k = mcp_to_kicad_xy(x2_mm, y2_mm, page_h)
    node = [
        sym("wire"),
        [
            sym("pts"),
            [sym("xy"), round_mm(x1k), round_mm(y1k)],
            [sym("xy"), round_mm(x2k), round_mm(y2k)],
        ],
        [sym("stroke"), [sym("width"), 0], [sym("type"), sym("default")]],
        [sym("uuid"), str(uuid.uuid4())],
    ]
    tree.append(node)
    return node


def add_label(
    tree: list,
    net_name: str,
    x_mm: float,
    y_mm: float,
    orientation: str = "right",
) -> list:
    """Append a (label ...) at a point. orientation ∈ {right, up, left, down}."""
    page_h = page_height_mm(tree)
    xk, yk = mcp_to_kicad_xy(x_mm, y_mm, page_h)
    angle_map = {"right": 0, "up": 90, "left": 180, "down": 270}
    if orientation not in angle_map:
        raise ValueError(f"orientation must be one of {list(angle_map)}")
    node = [
        sym("label"),
        net_name,
        [sym("at"), round_mm(xk), round_mm(yk), angle_map[orientation]],
        [
            sym("effects"),
            [sym("font"), [sym("size"), 1.27, 1.27]],
            [sym("justify"), sym("left"), sym("bottom")],
        ],
        [sym("uuid"), str(uuid.uuid4())],
    ]
    tree.append(node)
    return node


def add_no_connect(tree: list, x_mm: float, y_mm: float) -> list:
    """Append a (no_connect ...) marker at a point."""
    page_h = page_height_mm(tree)
    xk, yk = mcp_to_kicad_xy(x_mm, y_mm, page_h)
    node = [
        sym("no_connect"),
        [sym("at"), round_mm(xk), round_mm(yk)],
        [sym("uuid"), str(uuid.uuid4())],
    ]
    tree.append(node)
    return node


# --------------------------------------------------------------------------- #
# Buses (visual grouping of multiple nets)
# --------------------------------------------------------------------------- #


def add_bus_segment(
    tree: list,
    x1_mm: float,
    y1_mm: float,
    x2_mm: float,
    y2_mm: float,
) -> list:
    """Append a `(bus ...)` line — visually identical to a wire but thicker."""
    page_h = page_height_mm(tree)
    x1k, y1k = mcp_to_kicad_xy(x1_mm, y1_mm, page_h)
    x2k, y2k = mcp_to_kicad_xy(x2_mm, y2_mm, page_h)
    node = [
        sym("bus"),
        [
            sym("pts"),
            [sym("xy"), round_mm(x1k), round_mm(y1k)],
            [sym("xy"), round_mm(x2k), round_mm(y2k)],
        ],
        [sym("stroke"), [sym("width"), 0], [sym("type"), sym("default")]],
        [sym("uuid"), str(uuid.uuid4())],
    ]
    tree.append(node)
    return node


def add_bus_entry(
    tree: list,
    x_mm: float,
    y_mm: float,
    direction: str = "right_down",
) -> list:
    """Append a `(bus_entry ...)` — the diagonal line connecting a bus to a wire.

    `direction` controls the size vector (the offset from `at` to the wire side):
        "right_down" → (+2.54, +2.54)  (default; bus above-left, wire below-right)
        "right_up"   → (+2.54, -2.54)
        "left_down"  → (-2.54, +2.54)
        "left_up"    → (-2.54, -2.54)
    """
    deltas = {
        "right_down": (2.54, 2.54),
        "right_up": (2.54, -2.54),
        "left_down": (-2.54, 2.54),
        "left_up": (-2.54, -2.54),
    }
    if direction not in deltas:
        raise ValueError(
            f"direction must be one of {list(deltas)} (got {direction!r})"
        )
    dx, dy = deltas[direction]
    page_h = page_height_mm(tree)
    xk, yk = mcp_to_kicad_xy(x_mm, y_mm, page_h)
    node = [
        sym("bus_entry"),
        [sym("at"), round_mm(xk), round_mm(yk)],
        [sym("size"), dx, dy],
        [sym("stroke"), [sym("width"), 0], [sym("type"), sym("default")]],
        [sym("uuid"), str(uuid.uuid4())],
    ]
    tree.append(node)
    return node


def add_bus_alias(tree: list, alias_name: str, members: list[str]) -> list:
    """Declare `(bus_alias "NAME" (members "M0" "M1" ...))`.

    Aliases let you label a bus with a short name on the wire (e.g. "MEM")
    instead of writing out `MEM[0..7]`. Members must be exact net names.
    """
    if not alias_name:
        raise ValueError("alias_name must be non-empty")
    if not members:
        raise ValueError("alias needs at least one member net")
    members_node: list = [sym("members")]
    for m in members:
        members_node.append(str(m))
    node = [sym("bus_alias"), alias_name, members_node]
    tree.append(node)
    return node


# --------------------------------------------------------------------------- #
# Hierarchical sheets
# --------------------------------------------------------------------------- #


_LABEL_ORIENTATIONS = {"right": 0, "up": 90, "left": 180, "down": 270}
_VALID_LABEL_SHAPES = {"input", "output", "bidirectional", "tri_state", "passive"}


def add_sheet_node(
    parent_tree: list,
    *,
    sheet_name: str,
    sheet_filename: str,
    x_mm: float,
    y_mm: float,
    width_mm: float,
    height_mm: float,
    project_name: str,
) -> list:
    """Append a `(sheet ...)` placeholder to the parent (root) schematic.

    The placeholder references `sheet_filename` (relative path) and is sized
    `width_mm × height_mm`. Returns the new node.

    The bottom-left corner of the sheet sits at MCP (`x_mm`, `y_mm`).
    """
    if find_sheet_by_filename(parent_tree, sheet_filename) is not None:
        raise ValueError(f"sheet {sheet_filename!r} already registered")
    if find_sheet_by_name(parent_tree, sheet_name) is not None:
        raise ValueError(f"sheet name {sheet_name!r} already in use")

    page_h = page_height_mm(parent_tree)
    # KiCAD's sheet block uses the TOP-LEFT corner as its (at ...) origin.
    # Convert MCP bottom-left + size → KiCAD top-left.
    bl_k = mcp_to_kicad_xy(x_mm, y_mm, page_h)
    top_left_kicad = (bl_k[0], bl_k[1] - height_mm)
    root_uuid = _schematic_uuid(parent_tree)
    sheet_uuid = str(uuid.uuid4())
    page_no = len(find_children(parent_tree, "sheet")) + 2  # the root is page 1

    def _prop(name: str, value: str, dy: float, hide: bool) -> list:
        node: list = [
            sym("property"),
            name,
            value,
            [sym("at"), round_mm(top_left_kicad[0]), round_mm(top_left_kicad[1] + dy), 0],
        ]
        effects: list = [sym("effects"), [sym("font"), [sym("size"), 1.27, 1.27]],
                         [sym("justify"), sym("left"), sym("bottom")]]
        if hide:
            effects.append([sym("hide"), sym("yes")])
        node.append(effects)
        return node

    sheet_node = [
        sym("sheet"),
        [sym("at"), round_mm(top_left_kicad[0]), round_mm(top_left_kicad[1])],
        [sym("size"), round_mm(width_mm), round_mm(height_mm)],
        [sym("fields_autoplaced"), sym("yes")],
        [sym("stroke"), [sym("width"), 0.1524], [sym("type"), sym("solid")]],
        [sym("fill"), [sym("color"), 0, 0, 0, 0.0]],
        [sym("uuid"), sheet_uuid],
        _prop("Sheetname", sheet_name, -2.54, hide=False),
        _prop("Sheetfile", sheet_filename, height_mm + 1.27, hide=False),
        [
            sym("instances"),
            [
                sym("project"),
                project_name,
                [sym("path"), f"/{root_uuid}", [sym("page"), str(page_no)]],
            ],
        ],
    ]
    parent_tree.append(sheet_node)
    return sheet_node


def find_sheet_by_filename(tree: list, filename: str) -> list | None:
    """Find a `(sheet ...)` node by its Sheetfile property."""
    for node in tree[1:]:
        if not is_call(node, "sheet"):
            continue
        for prop in find_children(node, "property"):
            if (
                len(prop) >= 3
                and prop[1] == "Sheetfile"
                and prop[2] == filename
            ):
                return node
    return None


def find_sheet_by_name(tree: list, sheet_name: str) -> list | None:
    """Find a `(sheet ...)` node by its Sheetname property."""
    for node in tree[1:]:
        if not is_call(node, "sheet"):
            continue
        for prop in find_children(node, "property"):
            if (
                len(prop) >= 3
                and prop[1] == "Sheetname"
                and prop[2] == sheet_name
            ):
                return node
    return None


def get_sheet_uuid(sheet_node: list) -> str:
    u = find_child(sheet_node, "uuid")
    if not u or len(u) < 2 or not isinstance(u[1], str):
        raise ValueError("sheet has no uuid")
    return u[1]


def get_sheet_filename(sheet_node: list) -> str | None:
    for prop in find_children(sheet_node, "property"):
        if len(prop) >= 3 and prop[1] == "Sheetfile" and isinstance(prop[2], str):
            return prop[2]
    return None


def list_sheets(tree: list) -> list[dict]:
    """Return summaries of every (sheet ...) node in `tree`."""
    out = []
    for node in tree[1:]:
        if not is_call(node, "sheet"):
            continue
        name = ""
        filename = ""
        for prop in find_children(node, "property"):
            if len(prop) >= 3:
                if prop[1] == "Sheetname":
                    name = prop[2]
                elif prop[1] == "Sheetfile":
                    filename = prop[2]
        u = find_child(node, "uuid")
        sheet_uuid = u[1] if u and len(u) >= 2 else ""
        out.append({"name": name, "filename": filename, "uuid": sheet_uuid})
    return out


def add_hierarchical_label(
    tree: list,
    *,
    net_name: str,
    x_mm: float,
    y_mm: float,
    shape: str = "input",
    orientation: str = "right",
) -> list:
    """Append a `(hierarchical_label ...)` to the active sheet's tree."""
    if shape not in _VALID_LABEL_SHAPES:
        raise ValueError(f"shape must be one of {sorted(_VALID_LABEL_SHAPES)}")
    if orientation not in _LABEL_ORIENTATIONS:
        raise ValueError(
            f"orientation must be one of {list(_LABEL_ORIENTATIONS)}"
        )
    page_h = page_height_mm(tree)
    xk, yk = mcp_to_kicad_xy(x_mm, y_mm, page_h)
    node = [
        sym("hierarchical_label"),
        net_name,
        [sym("shape"), sym(shape)],
        [sym("at"), round_mm(xk), round_mm(yk), _LABEL_ORIENTATIONS[orientation]],
        [
            sym("effects"),
            [sym("font"), [sym("size"), 1.27, 1.27]],
            [sym("justify"), sym("left")],
        ],
        [sym("uuid"), str(uuid.uuid4())],
    ]
    tree.append(node)
    return node


def add_sheet_pin(
    sheet_node: list,
    *,
    pin_name: str,
    shape: str,
    x_mm: float,
    y_mm: float,
    page_h: float,
    orientation: str = "right",
) -> list:
    """Append a `(pin ...)` to a parent's `(sheet ...)` block.

    The pin's name must match a `(hierarchical_label ...)` of the same name
    inside the child schematic — that's how KiCAD wires the parent net into
    the child.
    """
    if shape not in _VALID_LABEL_SHAPES:
        raise ValueError(f"shape must be one of {sorted(_VALID_LABEL_SHAPES)}")
    if orientation not in _LABEL_ORIENTATIONS:
        raise ValueError(
            f"orientation must be one of {list(_LABEL_ORIENTATIONS)}"
        )
    xk, yk = mcp_to_kicad_xy(x_mm, y_mm, page_h)
    pin_node = [
        sym("pin"),
        pin_name,
        sym(shape),
        [sym("at"), round_mm(xk), round_mm(yk), _LABEL_ORIENTATIONS[orientation]],
        [
            sym("effects"),
            [sym("font"), [sym("size"), 1.27, 1.27]],
            [sym("justify"), sym("right")],
        ],
        [sym("uuid"), str(uuid.uuid4())],
    ]
    sheet_node.append(pin_node)
    return pin_node


# --------------------------------------------------------------------------- #
# Symbol fields (properties)
# --------------------------------------------------------------------------- #


def _find_property(symbol_node: list, name: str) -> list | None:
    for prop in find_children(symbol_node, "property"):
        if len(prop) >= 3 and prop[1] == name:
            return prop
    return None


def _make_property(name: str, value: str, x_k: float, y_k: float, *, hide: bool) -> list:
    effects: list[Any] = [sym("effects"), [sym("font"), [sym("size"), 1.27, 1.27]]]
    if hide:
        effects.append([sym("hide"), sym("yes")])
    return [sym("property"), name, value, [sym("at"), x_k, y_k, 0], effects]


def _is_hidden(prop: list) -> bool:
    """True if a property is hidden (KiCAD 9- puts `hide` in effects, 10 on the property)."""
    for holder in (prop, find_child(prop, "effects") or []):
        for child in holder[1:]:
            if child == sym("hide"):
                return True
            if is_call(child, "hide"):
                return len(child) < 2 or child[1] != sym("no")
    return False


def _set_hidden(prop: list, hidden: bool) -> None:
    for holder in (prop, find_child(prop, "effects") or []):
        holder[:] = [c for c in holder if not (c == sym("hide") or is_call(c, "hide"))]
    if hidden:
        effects = find_child(prop, "effects")
        if effects is None:
            effects = [sym("effects"), [sym("font"), [sym("size"), 1.27, 1.27]]]
            prop.append(effects)
        effects.append([sym("hide"), sym("yes")])


def _set_justify(prop: list, justify: str | None) -> None:
    effects = find_child(prop, "effects")
    if effects is None:
        effects = [sym("effects"), [sym("font"), [sym("size"), 1.27, 1.27]]]
        prop.append(effects)
    effects[:] = [c for c in effects if not is_call(c, "justify")]
    if justify:
        effects.append([sym("justify"), sym(justify)])


def is_power_symbol(symbol_def_node: list) -> bool:
    """True for library power symbols (`(power)` / `(power global)` flag)."""
    return find_child(symbol_def_node, "power") is not None


_FIELD_PITCH_MM = 2.54  # spacing between stacked Reference/Value lines
_FIELD_PAD_MM = 0.635  # gap between the symbol's bbox and the field text
_TEXT_HALF_H_MM = 0.635  # half the default 1.27 mm text height
_SIDE_BY_OUTWARD = {0: "right", 90: "top", 180: "left", 270: "bottom"}


def _lib_to_world(lx: float, ly: float, rot: float) -> tuple[float, float]:
    """Offset in lib coords (Y up) → offset from the symbol origin in KiCAD coords (Y down)."""
    rx, ry = rotate_xy(lx, ly, rot)
    return rx, -ry


def _draw_units(symbol_def_node: list, unit: int = 1):
    """Yield the symbol def and its sub-units drawn for `unit`, body style 1."""
    yield symbol_def_node
    for child in symbol_def_node[2:]:
        if is_call(child, "symbol") and len(child) >= 2 and isinstance(child[1], str):
            parts = child[1].rsplit("_", 2)
            if (
                len(parts) == 3
                and parts[1] in ("0", str(unit))
                and parts[2] in ("0", "1")
            ):
                yield child


def _symbol_local_points(symbol_def_node: list, unit: int = 1) -> list[tuple[float, float]]:
    """Outline points (lib coords) of body graphics and pins, for the bbox."""
    pts: list[tuple[float, float]] = []

    def _xy(node: list | None) -> tuple[float, float] | None:
        if node is None or len(node) < 3:
            return None
        return float(node[1]), float(node[2])

    for part in _draw_units(symbol_def_node, unit):
        for item in part[1:]:
            h = head_of(item)
            if h == "rectangle":
                pts += [p for p in (_xy(find_child(item, "start")), _xy(find_child(item, "end"))) if p]
            elif h in ("polyline", "bezier"):
                p_node = find_child(item, "pts")
                pts += [_xy(xy) for xy in find_children(p_node, "xy")] if p_node else []
            elif h == "circle":
                c = _xy(find_child(item, "center"))
                r_node = find_child(item, "radius")
                if c and r_node:
                    r = float(r_node[1])
                    pts += [(c[0] - r, c[1] - r), (c[0] + r, c[1] + r)]
            elif h == "arc":
                pts += [p for p in (_xy(find_child(item, k)) for k in ("start", "mid", "end")) if p]
            elif h == "pin":
                x, y, a = _pin_local_at(item)
                ln = find_child(item, "length")
                length = float(ln[1]) if ln else 0.0
                dx, dy = rotate_xy(length, 0, a)
                pts += [(x, y), (x + dx, y + dy)]
    return pts


def _pin_sides(symbol_def_node: list, rot: float, unit: int = 1) -> set[str]:
    """World sides ({right, top, left, bottom}) that have pins sticking out."""
    sides = set()
    for pin_node, _unit in symbol_lib.pins_of_unit(symbol_def_node, unit):
        # A pin's angle points from its tip into the body; outward is opposite.
        outward = int(round(_pin_local_at(pin_node)[2] + 180 + rot)) % 360
        if outward in _SIDE_BY_OUTWARD:
            sides.add(_SIDE_BY_OUTWARD[outward])
    return sides


def _snap_out(value: float, direction: int, grid: float = SCH_GRID_MM) -> float:
    """Round `value` to the grid, away from the body (direction +1 = up the axis)."""
    q = value / grid
    return round((math.ceil(q - 1e-6) if direction > 0 else math.floor(q + 1e-6)) * grid, 4)


# A field's angle and justification are stored in the symbol's local frame, so
# KiCAD rotates them with the symbol. To keep text horizontal and justified as
# intended on screen, compensate: 90°/270° symbols get 90° field text, and the
# horizontal justification flips for the rotations that mirror it on screen.
_JUSTIFY_FLIP_BY_ROT = {0: False, 90: True, 180: True, 270: False}
_FLIPPED = {"left": "right", "right": "left"}


def autoplace_fields(s_node: list, symbol_def_node: list) -> None:
    """Place Reference/Value next to the symbol body, roughly as KiCAD's autoplacer.

    Power symbols keep the library's field positions (rotated with the
    symbol). Other symbols get their visible Reference/Value stacked on the
    first side with no pins, in the order right, top, bottom, left. Text is
    always horizontal on screen regardless of the symbol rotation.
    """
    at = find_child(s_node, "at")
    sx, sy, rot = float(at[1]), float(at[2]), int(at[3]) % 360
    local_angle = 90 if rot in (90, 270) else 0

    if is_power_symbol(symbol_def_node):
        for prop in find_children(s_node, "property"):
            lib_prop = _find_property(symbol_def_node, prop[1])
            lib_at = find_child(lib_prop, "at") if lib_prop else None
            if lib_at is None or len(lib_at) < 3:
                continue
            ox, oy = _lib_to_world(float(lib_at[1]), float(lib_at[2]), rot)
            p_at = find_child(prop, "at")
            p_at[1], p_at[2] = round_mm(sx + ox), round_mm(sy + oy)
            p_at[3] = float(lib_at[3]) if len(lib_at) >= 4 else 0
        return

    unit = symbol_unit(s_node)
    world = [_lib_to_world(x, y, rot) for x, y in _symbol_local_points(symbol_def_node, unit)]
    if not world:
        world = [(-1.27, -1.27), (1.27, 1.27)]
    left, right = min(p[0] for p in world), max(p[0] for p in world)
    top, bottom = min(p[1] for p in world), max(p[1] for p in world)

    fields = [
        p for p in (_find_property(s_node, n) for n in ("Reference", "Value"))
        if p is not None and not _is_hidden(p)
    ]
    sides = _pin_sides(symbol_def_node, rot, unit)
    side = next((sd for sd in ("right", "top", "bottom", "left") if sd not in sides), "top")
    n = len(fields)

    placements: list[tuple[float, float, str | None]] = []
    if side in ("right", "left"):
        if side == "right":
            x = _snap_out(right + _FIELD_PAD_MM, +1)
        else:
            x = _snap_out(left - _FIELD_PAD_MM, -1)
        cy = (top + bottom) / 2
        for i in range(n):
            y = round(round((cy + (i - (n - 1) / 2) * _FIELD_PITCH_MM) / SCH_GRID_MM) * SCH_GRID_MM, 4)
            placements.append((x, y, "left" if side == "right" else "right"))
    else:
        cx = round(round((left + right) / 2 / SCH_GRID_MM) * SCH_GRID_MM, 4)
        if side == "top":
            # Last field sits closest to the body; earlier ones stack above it.
            y0 = _snap_out(top - _FIELD_PAD_MM - _TEXT_HALF_H_MM, -1)
            ys = [y0 - (n - 1 - i) * _FIELD_PITCH_MM for i in range(n)]
        else:
            y0 = _snap_out(bottom + _FIELD_PAD_MM + _TEXT_HALF_H_MM, +1)
            ys = [y0 + i * _FIELD_PITCH_MM for i in range(n)]
        placements = [(cx, y, None) for y in ys]

    for prop, (ox, oy, justify) in zip(fields, placements):
        p_at = find_child(prop, "at")
        p_at[1], p_at[2], p_at[3] = round_mm(sx + ox), round_mm(sy + oy), local_angle
        if justify and _JUSTIFY_FLIP_BY_ROT[rot]:
            justify = _FLIPPED[justify]
        _set_justify(prop, justify)

    # Hidden fields follow the symbol origin so they never linger off-page.
    for prop in find_children(s_node, "property"):
        if prop not in fields:
            p_at = find_child(prop, "at")
            p_at[1], p_at[2], p_at[3] = round_mm(sx), round_mm(sy), local_angle

    if find_child(s_node, "fields_autoplaced") is None:
        s_node.insert(s_node.index(find_child(s_node, "uuid")), [sym("fields_autoplaced"), sym("yes")])


# --------------------------------------------------------------------------- #
# Pin position math
# --------------------------------------------------------------------------- #


def find_lib_symbol_def(tree: list, qualified_lib_id: str) -> list | None:
    """Look up the lib_symbols entry for a qualified id within the schematic."""
    block = find_child(tree, "lib_symbols")
    if not block:
        return None
    for child in block[1:]:
        if is_call(child, "symbol") and len(child) >= 2 and child[1] == qualified_lib_id:
            return child
    return None


def _iter_pins(symbol_def_node: list):
    """Yield (pin_node, parent_unit_node_or_None) for every pin in a lib symbol def."""
    for child in symbol_def_node[2:]:
        h = head_of(child)
        if h == "pin":
            yield child, None
        elif h == "symbol":
            for sub in child[2:]:
                if head_of(sub) == "pin":
                    yield sub, child


def _pin_local_at(pin_node: list) -> tuple[float, float, float]:
    """Return (x, y, angle) from `(pin ... (at x y angle) ...)` in lib coords (Y down)."""
    at = find_child(pin_node, "at")
    if not at or len(at) < 4:
        return 0.0, 0.0, 0.0
    return float(at[1]), float(at[2]), float(at[3])


def _pin_id(pin_node: list) -> tuple[str, str]:
    """Return (number, name) for a pin node."""
    number = ""
    name = ""
    for sub in pin_node[1:]:
        if is_call(sub, "number") and len(sub) >= 2 and isinstance(sub[1], str):
            number = sub[1]
        elif is_call(sub, "name") and len(sub) >= 2 and isinstance(sub[1], str):
            name = sub[1]
    return number, name


def _unit_pin_entries(
    tree: list, s_node: list, sym_def: list, reference: str, page_h: float
) -> list[dict]:
    """Pins of one placed unit with absolute MCP positions."""
    at = find_child(s_node, "at")
    if not at or len(at) < 4:
        raise ValueError(f"symbol {reference!r} has malformed (at ...)")
    sx, sy, srot = float(at[1]), float(at[2]), float(at[3])
    unit = symbol_unit(s_node)
    out = []
    for pin_node, _u in symbol_lib.pins_of_unit(sym_def, unit):
        lx, ly, lrot = _pin_local_at(pin_node)
        # KiCAD rotation is CCW and the library Y axis points up; the file's
        # schematic Y points down, hence the flip on the way out.
        rx, ry = rotate_xy(lx, ly, srot)
        mcp_x, mcp_y = sx + rx, page_h - (sy - ry)
        number, name = _pin_id(pin_node)
        out.append(
            {
                "number": number,
                "name": name,
                "unit": unit,
                "position_mm": [round_mm(mcp_x), round_mm(mcp_y)],
                "angle": (lrot + srot) % 360,
            }
        )
    return out


def list_pins_for_symbol(
    tree: list, reference: str, unit: int | None = None
) -> list[dict]:
    """List pins of a placed symbol with their absolute positions in MCP coords.

    For a multi-unit part this covers every placed unit (or just `unit`);
    unplaced units have no position and are not listed.
    """
    nodes = [
        n for n in find_symbols_by_reference(tree, reference)
        if unit is None or symbol_unit(n) == unit
    ]
    if not nodes:
        raise KeyError(f"no symbol with reference {reference!r}")
    page_h = page_height_mm(tree)
    out: list[dict] = []
    for s_node in nodes:
        lib_id_node = find_child(s_node, "lib_id")
        if not lib_id_node or len(lib_id_node) < 2:
            raise ValueError(f"symbol {reference!r} has no lib_id")
        sym_def = find_lib_symbol_def(tree, lib_id_node[1])
        if sym_def is None:
            raise KeyError(
                f"lib_symbols entry {lib_id_node[1]!r} missing — was the schematic written by us?"
            )
        out += _unit_pin_entries(tree, s_node, sym_def, reference, page_h)
    return out


def get_pin_position(
    tree: list, reference: str, pin_number: str, unit: int | None = None
) -> tuple[float, float]:
    """Return (x, y) in MCP coords for a specific pin of a placed symbol."""
    pins = list_pins_for_symbol(tree, reference, unit)
    for p in pins:
        if p["number"] == pin_number:
            return tuple(p["position_mm"])  # type: ignore[return-value]
    raise KeyError(
        f"pin {pin_number!r} not found on {reference!r}"
        f"{'' if unit is None else f' unit {unit}'}; "
        f"available: {[p['number'] for p in pins]}"
    )


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #


def _schematic_uuid(tree: list) -> str:
    u = find_child(tree, "uuid")
    if u and len(u) >= 2 and isinstance(u[1], str):
        return u[1]
    new = str(uuid.uuid4())
    tree.insert(1, [sym("uuid"), new])
    return new
