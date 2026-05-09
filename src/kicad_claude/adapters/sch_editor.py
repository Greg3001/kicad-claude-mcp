"""High-level mutations on a parsed `.kicad_sch` tree.

Operates on raw sexpdata trees — `parse_file` from `sch_io.py` returns the
top-level list, and these helpers mutate it in place. Use `write_file` to
serialize back.

Coordinate convention: all `x_mm`/`y_mm` parameters are in **MCP coordinates**
(Y up). Internal storage is in KiCAD coordinates (Y down). The `_mcp_to_at`
helper does the conversion at the boundary.
"""

from __future__ import annotations

import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import sexpdata

from kicad_claude.adapters import sch_io
from kicad_claude.adapters.sch_io import (
    find_child,
    find_children,
    head_of,
    is_call,
    sym,
)
from kicad_claude.utils.geometry import (
    DEFAULT_PAGE_HEIGHT_MM,
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
    """Detect the schematic page height. Defaults to A4 landscape (210mm)."""
    paper = find_child(tree, "paper")
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


def find_symbol_by_reference(tree: list, reference: str) -> list | None:
    for s_node in iter_instance_symbols(tree):
        if get_symbol_property(s_node, "Reference") == reference:
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
    """Open a `.kicad_sym`, return a deep copy of the named (symbol ...) node.

    The returned node is renamed-ready for lib_symbols: the caller should
    set its name to "LibName:SymbolName" before injecting.
    """
    import copy

    text = Path(lib_path).read_text(encoding="utf-8", errors="replace")
    data = sexpdata.loads(text)
    if not is_call(data, "kicad_symbol_lib"):
        raise ValueError(f"not a kicad_symbol_lib: {lib_path}")
    for child in data[1:]:
        if is_call(child, "symbol") and len(child) >= 2 and child[1] == symbol_name:
            return copy.deepcopy(child)
    raise KeyError(f"symbol {symbol_name!r} not found in {lib_path}")


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
    schematic_uuid: str,
    page_h: float,
    footprint: str = "",
    datasheet: str = "~",
    description: str = "",
) -> list:
    """Construct a new (symbol ...) instance node ready to inject into the schematic."""
    x_k, y_k = mcp_to_kicad_xy(x_mcp, y_mcp, page_h)
    x_k, y_k = round_mm(x_k), round_mm(y_k)

    inst_uuid = str(uuid.uuid4())

    # Properties — text positioned at the symbol origin; KiCAD will adjust on first
    # render. We hide Footprint/Datasheet/Description since they are mostly metadata.
    def _prop(name: str, val: str, hide: bool) -> list:
        node: list[Any] = [
            sym("property"),
            name,
            val,
            [sym("at"), x_k, y_k, 0],
        ]
        effects: list[Any] = [sym("effects"), [sym("font"), [sym("size"), 1.27, 1.27]]]
        if hide:
            effects.append([sym("hide"), sym("yes")])
        node.append(effects)
        return node

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
                f"/{schematic_uuid}",
                [sym("reference"), reference],
                [sym("unit"), 1],
            ],
        ],
    ]

    return [
        sym("symbol"),
        [sym("lib_id"), qualified_lib_id],
        [sym("at"), x_k, y_k, rotation_deg],
        [sym("unit"), 1],
        [sym("exclude_from_sim"), sym("no")],
        [sym("in_bom"), sym("yes")],
        [sym("on_board"), sym("yes")],
        [sym("dnp"), sym("no")],
        [sym("uuid"), inst_uuid],
        _prop("Reference", reference, hide=False),
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
    footprint: str = "",
    datasheet: str = "~",
    description: str = "",
) -> list:
    """Inject a symbol into the schematic. Returns the new (symbol ...) node."""
    if find_symbol_by_reference(tree, reference) is not None:
        raise ValueError(f"reference {reference!r} already exists")

    rot = normalize_rotation(rotation)
    page_h = page_height_mm(tree)
    schematic_uuid = _schematic_uuid(tree)

    # Inject the lib symbol definition (idempotent on qualified id).
    lib_entry_def = make_lib_symbol_entry(sym_def_node, qualified_lib_id)
    inject_lib_symbol(tree, lib_entry_def)

    pins = collect_pin_numbers(lib_entry_def)
    instance = build_symbol_instance(
        qualified_lib_id=qualified_lib_id,
        reference=reference,
        value=value,
        x_mcp=x_mm,
        y_mcp=y_mm,
        rotation_deg=rot,
        pin_numbers=pins,
        project_name=project_name,
        schematic_uuid=schematic_uuid,
        page_h=page_h,
        footprint=footprint,
        datasheet=datasheet,
        description=description,
    )
    tree.append(instance)
    return instance


def remove_symbol(tree: list, reference: str) -> bool:
    """Remove the first symbol matching `reference`. Returns True if removed."""
    for i, child in enumerate(tree):
        if (
            is_call(child, "symbol")
            and find_child(child, "lib_id")
            and get_symbol_property(child, "Reference") == reference
        ):
            tree.pop(i)
            return True
    return False


def move_symbol(
    tree: list,
    reference: str,
    x_mm: float,
    y_mm: float,
    rotation: float | None = None,
) -> None:
    """Set absolute position (and optionally rotation) of an existing symbol."""
    s_node = find_symbol_by_reference(tree, reference)
    if s_node is None:
        raise KeyError(f"no symbol with reference {reference!r}")
    page_h = page_height_mm(tree)
    x_k, y_k = mcp_to_kicad_xy(x_mm, y_mm, page_h)
    at = find_child(s_node, "at")
    if at is None or len(at) < 4:
        raise ValueError("symbol has malformed (at ...) node")
    at[1] = round_mm(x_k)
    at[2] = round_mm(y_k)
    if rotation is not None:
        at[3] = normalize_rotation(rotation)


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


def list_pins_for_symbol(tree: list, reference: str) -> list[dict]:
    """List pins of a placed symbol with their absolute positions in MCP coords."""
    s_node = find_symbol_by_reference(tree, reference)
    if s_node is None:
        raise KeyError(f"no symbol with reference {reference!r}")
    lib_id_node = find_child(s_node, "lib_id")
    if not lib_id_node or len(lib_id_node) < 2:
        raise ValueError(f"symbol {reference!r} has no lib_id")
    qualified = lib_id_node[1]
    sym_def = find_lib_symbol_def(tree, qualified)
    if sym_def is None:
        raise KeyError(
            f"lib_symbols entry {qualified!r} missing — was the schematic written by us?"
        )

    at = find_child(s_node, "at")
    if not at or len(at) < 4:
        raise ValueError(f"symbol {reference!r} has malformed (at ...)")
    sx, sy, srot = float(at[1]), float(at[2]), float(at[3])
    page_h = page_height_mm(tree)

    out = []
    for pin_node, _unit in _iter_pins(sym_def):
        lx, ly, lrot = _pin_local_at(pin_node)
        # KiCAD rotation is CCW. Library coords have Y down; instance rotation
        # is also applied in those coords.
        rx, ry = rotate_xy(lx, ly, srot)
        # In KiCAD, pin local Y has the same orientation as schematic Y (both
        # "down"), but rotate_xy uses math convention (Y up). Since both
        # systems are consistent, rotate_xy + add gives the right result.
        wx_kicad = sx + rx
        wy_kicad = sy - ry  # flip because KiCAD Y is down vs math Y up
        mcp_x, mcp_y = wx_kicad, page_h - wy_kicad
        number, name = _pin_id(pin_node)
        out.append(
            {
                "number": number,
                "name": name,
                "position_mm": [round_mm(mcp_x), round_mm(mcp_y)],
                "angle": (lrot + srot) % 360,
            }
        )
    return out


def get_pin_position(tree: list, reference: str, pin_number: str) -> tuple[float, float]:
    """Return (x, y) in MCP coords for a specific pin of a placed symbol."""
    pins = list_pins_for_symbol(tree, reference)
    for p in pins:
        if p["number"] == pin_number:
            return tuple(p["position_mm"])  # type: ignore[return-value]
    raise KeyError(
        f"pin {pin_number!r} not found on {reference!r}; available: {[p['number'] for p in pins]}"
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
