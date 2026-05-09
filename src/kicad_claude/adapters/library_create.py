"""Build new symbols and footprints from scratch.

Two entry points:

    create_symbol(...)    appends a (symbol "Lib:Name" ...) into a project lib's
                          .kicad_sym file. Generates a rectangular body + pins
                          on the four edges (or wherever the caller specifies).

    create_footprint(...) writes a fresh .kicad_mod into <project>/lib/<Lib>.pretty/.
                          Pads can be SMD or through-hole, with auto-courtyard.

Both register the lib in the project's sym-lib-table / fp-lib-table so KiCAD
sees the new entries on next open.
"""

from __future__ import annotations

import logging
import uuid as _uuid
from pathlib import Path
from typing import Any

from kicad_claude.adapters import sch_io, vendor_import
from kicad_claude.adapters.sch_io import is_call, sym

logger = logging.getLogger("kicad-claude.adapters.library_create")


# Valid pin electrical types (from KiCAD symbol format spec)
PIN_TYPES = frozenset({
    "input", "output", "bidirectional", "tri_state", "passive",
    "free", "unspecified", "power_in", "power_out",
    "open_collector", "open_emitter", "no_connect",
})

PIN_SHAPES = frozenset({
    "line", "inverted", "clock", "inverted_clock",
    "input_low", "clock_low", "output_low", "edge_clock_high",
    "non_logic",
})

# Pad types and shapes
PAD_TYPES = frozenset({"smd", "thru_hole", "np_thru_hole", "connect"})
PAD_SHAPES = frozenset({
    "circle", "rect", "oval", "roundrect", "trapezoid", "custom",
})


# --------------------------------------------------------------------------- #
# Symbol creation
# --------------------------------------------------------------------------- #


def _build_pin_node(
    *,
    number: str,
    name: str,
    x_mm: float,
    y_mm: float,
    length_mm: float,
    angle_deg: int,
    pin_type: str,
    pin_shape: str,
) -> list:
    if pin_type not in PIN_TYPES:
        raise ValueError(
            f"pin_type must be one of {sorted(PIN_TYPES)} (got {pin_type!r})"
        )
    if pin_shape not in PIN_SHAPES:
        raise ValueError(
            f"pin_shape must be one of {sorted(PIN_SHAPES)} (got {pin_shape!r})"
        )
    return [
        sym("pin"), sym(pin_type), sym(pin_shape),
        [sym("at"), float(x_mm), float(y_mm), int(angle_deg)],
        [sym("length"), float(length_mm)],
        [sym("name"), name, [sym("effects"), [sym("font"), [sym("size"), 1.27, 1.27]]]],
        [sym("number"), str(number),
         [sym("effects"), [sym("font"), [sym("size"), 1.27, 1.27]]]],
    ]


def _build_property_node(
    name: str, value: str, *, x: float = 0, y: float = 0, hide: bool = False
) -> list:
    effects: list = [sym("effects"), [sym("font"), [sym("size"), 1.27, 1.27]]]
    if hide:
        effects.append([sym("hide"), sym("yes")])
    return [
        sym("property"), name, value,
        [sym("at"), float(x), float(y), 0],
        effects,
    ]


def build_symbol_node(
    *,
    qualified_lib_id: str,            # e.g. "vendor:MY_PART"
    pins: list[dict],
    body_width_mm: float = 5.08,
    body_height_mm: float = 5.08,
    reference_prefix: str = "U",
    value: str = "",
    footprint: str = "",
    datasheet: str = "~",
    description: str = "",
    keywords: str = "",
) -> list:
    """Build a complete (symbol "Lib:Name" ...) tree.

    Body is a centered rectangle of body_width × body_height. Pins live in
    a sub-symbol (KiCAD's standard layout for graphics + pins).
    """
    half_w = body_width_mm / 2
    half_h = body_height_mm / 2

    # In a .kicad_sym source file the top-level (symbol "...") is the BARE
    # name (e.g. "S32K358"), not "Lib:S32K358". The qualifier is only used
    # when the symbol is copied into a schematic's lib_symbols block. Mixing
    # them up makes the indexer construct lib_ids like "haps_vendor:haps_vendor:S32K358"
    # since it prepends the lib filename's stem.
    bare_name = qualified_lib_id.split(":", 1)[-1] if ":" in qualified_lib_id else qualified_lib_id
    sub_sym_name = bare_name + "_0_1"

    body_rect = [
        sym("rectangle"),
        [sym("start"), -half_w, -half_h],
        [sym("end"), half_w, half_h],
        [sym("stroke"), [sym("width"), 0.254], [sym("type"), sym("default")]],
        [sym("fill"), [sym("type"), sym("background")]],
    ]

    pin_nodes: list[list] = []
    for pin in pins:
        pin_nodes.append(_build_pin_node(
            number=pin["number"],
            name=pin.get("name", "~"),
            x_mm=pin["x_mm"],
            y_mm=pin["y_mm"],
            length_mm=pin.get("length_mm", 2.54),
            angle_deg=pin.get("angle_deg", pin.get("angle", 0)),
            pin_type=pin.get("type", "passive"),
            pin_shape=pin.get("shape", "line"),
        ))

    sub_symbol = [sym("symbol"), sub_sym_name, body_rect, *pin_nodes]

    symbol: list[Any] = [
        sym("symbol"), bare_name,
        [sym("pin_numbers"), [sym("hide"), sym("no")]],
        [sym("pin_names"), [sym("offset"), 1.016]],
        [sym("exclude_from_sim"), sym("no")],
        [sym("in_bom"), sym("yes")],
        [sym("on_board"), sym("yes")],
        _build_property_node("Reference", reference_prefix),
        _build_property_node("Value", value or qualified_lib_id.split(":", 1)[-1]),
        _build_property_node("Footprint", footprint, hide=True),
        _build_property_node("Datasheet", datasheet, hide=True),
        _build_property_node("Description", description, hide=True),
    ]
    if keywords:
        symbol.append(_build_property_node("ki_keywords", keywords, hide=True))
    symbol.append(sub_symbol)
    return symbol


def append_symbol_to_lib(lib_path: Path, symbol_node: list) -> None:
    """Append a built symbol node to a .kicad_sym lib (creating the lib if needed).

    Refuses to add if a symbol with the same qualified name already exists
    (the caller should remove it first).
    """
    bare = symbol_node[1] if len(symbol_node) >= 2 else None
    lib_path = Path(lib_path)
    lib_path.parent.mkdir(parents=True, exist_ok=True)

    if lib_path.is_file():
        tree = sch_io.parse_file(lib_path)
        if not is_call(tree, "kicad_symbol_lib"):
            raise ValueError(f"{lib_path} is not a kicad_symbol_lib")
    else:
        tree = [
            sym("kicad_symbol_lib"),
            [sym("version"), 20251024],
            [sym("generator"), "kicad-claude"],
            [sym("generator_version"), "0.1"],
        ]

    for child in tree[1:]:
        if (
            is_call(child, "symbol")
            and len(child) >= 2
            and child[1] == bare
        ):
            raise FileExistsError(
                f"symbol {bare!r} already exists in {lib_path}; "
                "remove it manually or import via a different name."
            )
    tree.append(symbol_node)
    sch_io.write_file(lib_path, tree)


def create_symbol(
    project_dir: Path,
    *,
    lib_name: str,
    symbol_name: str,
    pins: list[dict],
    body_width_mm: float = 5.08,
    body_height_mm: float = 5.08,
    reference_prefix: str = "U",
    value: str = "",
    footprint: str = "",
    datasheet: str = "~",
    description: str = "",
    keywords: str = "",
) -> dict:
    """Top-level: write the symbol into <project>/lib/<lib_name>.kicad_sym
    and register the lib in sym-lib-table.

    Returns a dict with paths and the qualified lib_id.
    """
    project_dir = Path(project_dir)
    lib_path = project_dir / "lib" / f"{lib_name}.kicad_sym"
    qualified = f"{lib_name}:{symbol_name}"

    node = build_symbol_node(
        qualified_lib_id=qualified, pins=pins,
        body_width_mm=body_width_mm, body_height_mm=body_height_mm,
        reference_prefix=reference_prefix,
        value=value, footprint=footprint, datasheet=datasheet,
        description=description, keywords=keywords,
    )
    append_symbol_to_lib(lib_path, node)
    table_path = vendor_import.update_sym_lib_table(project_dir, lib_name)
    return {
        "lib_id": qualified,
        "lib_path": str(lib_path),
        "sym_lib_table": str(table_path),
        "pin_count": len(pins),
    }


# --------------------------------------------------------------------------- #
# Footprint creation
# --------------------------------------------------------------------------- #


def _build_pad_node(
    *,
    number: str,
    pad_type: str,
    pad_shape: str,
    x_mm: float, y_mm: float,
    size_x_mm: float, size_y_mm: float,
    drill_mm: float | None = None,
    layers: list[str] | None = None,
    rotation_deg: float = 0,
) -> list:
    if pad_type not in PAD_TYPES:
        raise ValueError(f"pad_type must be one of {sorted(PAD_TYPES)} (got {pad_type!r})")
    if pad_shape not in PAD_SHAPES:
        raise ValueError(f"pad_shape must be one of {sorted(PAD_SHAPES)} (got {pad_shape!r})")

    if layers is None:
        if pad_type == "smd":
            layers = ["F.Cu", "F.Paste", "F.Mask"]
        elif pad_type in ("thru_hole", "connect"):
            layers = ["*.Cu", "*.Mask"]
        elif pad_type == "np_thru_hole":
            layers = ["*.Mask"]

    if pad_type in ("thru_hole", "np_thru_hole") and drill_mm is None:
        # Sensible default: drill = min(size_x, size_y) - 0.4 mm (annular ring)
        drill_mm = max(0.3, min(size_x_mm, size_y_mm) - 0.4)

    node: list[Any] = [
        sym("pad"), str(number), sym(pad_type), sym(pad_shape),
        [sym("at"), float(x_mm), float(y_mm)] + (
            [float(rotation_deg)] if rotation_deg else []
        ),
        [sym("size"), float(size_x_mm), float(size_y_mm)],
    ]
    if drill_mm is not None:
        node.append([sym("drill"), float(drill_mm)])
    layers_node: list = [sym("layers")]
    for ly in layers:
        layers_node.append(ly)
    node.append(layers_node)
    node.append([sym("uuid"), str(_uuid.uuid4())])
    return node


def _bounding_box(pads: list[dict]) -> tuple[float, float, float, float]:
    """Min/max x/y across pads, accounting for pad sizes."""
    xs: list[float] = []
    ys: list[float] = []
    for pad in pads:
        cx, cy = pad["x_mm"], pad["y_mm"]
        sx, sy = pad["size_x_mm"], pad["size_y_mm"]
        xs += [cx - sx / 2, cx + sx / 2]
        ys += [cy - sy / 2, cy + sy / 2]
    return min(xs), min(ys), max(xs), max(ys)


def _build_courtyard_outline(pads: list[dict], inflate_mm: float = 0.25) -> list[list]:
    """Generate fp_line segments forming a rectangular courtyard around the pads."""
    if not pads:
        return []
    minx, miny, maxx, maxy = _bounding_box(pads)
    minx -= inflate_mm
    maxx += inflate_mm
    miny -= inflate_mm
    maxy += inflate_mm
    corners = [(minx, miny), (maxx, miny), (maxx, maxy), (minx, maxy), (minx, miny)]
    lines = []
    for i in range(4):
        s = corners[i]
        e = corners[i + 1]
        lines.append([
            sym("fp_line"),
            [sym("start"), s[0], s[1]],
            [sym("end"), e[0], e[1]],
            [sym("stroke"), [sym("width"), 0.05], [sym("type"), sym("default")]],
            [sym("layer"), "F.CrtYd"],
            [sym("uuid"), str(_uuid.uuid4())],
        ])
    return lines


def build_footprint_node(
    *,
    qualified_lib_id: str,
    pads: list[dict],
    description: str = "",
    tags: str = "",
    add_courtyard: bool = True,
    add_silk_outline: bool = True,
) -> list:
    """Build a footprint node (top-level (footprint ...)) for writing to .kicad_mod."""
    pad_nodes: list[list] = []
    for pad in pads:
        pad_nodes.append(_build_pad_node(
            number=pad.get("number", "1"),
            pad_type=pad.get("type", "smd"),
            pad_shape=pad.get("shape", "rect"),
            x_mm=pad["x_mm"], y_mm=pad["y_mm"],
            size_x_mm=pad["size_x_mm"], size_y_mm=pad["size_y_mm"],
            drill_mm=pad.get("drill_mm"),
            layers=pad.get("layers"),
            rotation_deg=pad.get("rotation_deg", 0),
        ))

    courtyard = _build_courtyard_outline(pads) if (add_courtyard and pads) else []

    silk_outline: list[list] = []
    if add_silk_outline and pads:
        minx, miny, maxx, maxy = _bounding_box(pads)
        # Silk slightly outside the pads — typical 0.15 mm
        infl = 0.15
        minx -= infl; maxx += infl; miny -= infl; maxy += infl
        for s, e in (
            ((minx, miny), (maxx, miny)),
            ((maxx, miny), (maxx, maxy)),
            ((maxx, maxy), (minx, maxy)),
            ((minx, maxy), (minx, miny)),
        ):
            silk_outline.append([
                sym("fp_line"),
                [sym("start"), s[0], s[1]],
                [sym("end"), e[0], e[1]],
                [sym("stroke"), [sym("width"), 0.12], [sym("type"), sym("default")]],
                [sym("layer"), "F.SilkS"],
                [sym("uuid"), str(_uuid.uuid4())],
            ])

    name_only = qualified_lib_id.split(":", 1)[-1] if ":" in qualified_lib_id else qualified_lib_id
    fp: list[Any] = [
        sym("footprint"), name_only,
        [sym("version"), 20240108],
        [sym("generator"), "kicad-claude"],
        [sym("generator_version"), "0.1"],
        [sym("layer"), "F.Cu"],
        [sym("descr"), description],
        [sym("tags"), tags],
        [
            sym("property"), "Reference", "REF**",
            [sym("at"), 0, -2, 0],
            [sym("layer"), "F.SilkS"],
            [sym("uuid"), str(_uuid.uuid4())],
            [sym("effects"), [sym("font"), [sym("size"), 1, 1], [sym("thickness"), 0.15]]],
        ],
        [
            sym("property"), "Value", name_only,
            [sym("at"), 0, 2, 0],
            [sym("layer"), "F.Fab"],
            [sym("uuid"), str(_uuid.uuid4())],
            [sym("effects"), [sym("font"), [sym("size"), 1, 1], [sym("thickness"), 0.15]]],
        ],
        [
            sym("property"), "Footprint", "",
            [sym("at"), 0, 0, 0],
            [sym("hide"), sym("yes")],
            [sym("layer"), "F.Fab"],
            [sym("uuid"), str(_uuid.uuid4())],
            [sym("effects"), [sym("font"), [sym("size"), 1.27, 1.27]]],
        ],
        [
            sym("property"), "Datasheet", "",
            [sym("at"), 0, 0, 0],
            [sym("hide"), sym("yes")],
            [sym("layer"), "F.Fab"],
            [sym("uuid"), str(_uuid.uuid4())],
            [sym("effects"), [sym("font"), [sym("size"), 1.27, 1.27]]],
        ],
        [
            sym("property"), "Description", description,
            [sym("at"), 0, 0, 0],
            [sym("hide"), sym("yes")],
            [sym("layer"), "F.Fab"],
            [sym("uuid"), str(_uuid.uuid4())],
            [sym("effects"), [sym("font"), [sym("size"), 1.27, 1.27]]],
        ],
        *courtyard,
        *silk_outline,
        *pad_nodes,
    ]
    return fp


def create_footprint(
    project_dir: Path,
    *,
    lib_name: str,
    footprint_name: str,
    pads: list[dict],
    description: str = "",
    tags: str = "",
    add_courtyard: bool = True,
    add_silk_outline: bool = True,
) -> dict:
    """Top-level: write a footprint to <project>/lib/<lib_name>.pretty/<footprint_name>.kicad_mod.

    Auto-courtyard (F.CrtYd) and auto-silk outline (F.SilkS) are inflated
    around the pad bounding box. Disable with add_courtyard=False or
    add_silk_outline=False.
    """
    project_dir = Path(project_dir)
    pretty_dir = project_dir / "lib" / f"{lib_name}.pretty"
    pretty_dir.mkdir(parents=True, exist_ok=True)
    target = pretty_dir / f"{footprint_name}.kicad_mod"
    if target.is_file():
        raise FileExistsError(f"{target} already exists")

    qualified = f"{lib_name}:{footprint_name}"
    node = build_footprint_node(
        qualified_lib_id=qualified, pads=pads,
        description=description, tags=tags,
        add_courtyard=add_courtyard,
        add_silk_outline=add_silk_outline,
    )
    sch_io.write_file(target, node)
    table_path = vendor_import.update_fp_lib_table(project_dir, lib_name)
    return {
        "lib_id": qualified,
        "kicad_mod_path": str(target),
        "fp_lib_table": str(table_path),
        "pad_count": len(pads),
    }
