"""High-level mutations on a parsed `.kicad_pcb` tree.

Mirrors `sch_editor` but for PCBs. Same s-expression machinery (`sch_io`),
same MCP coordinate convention (Y up; the boundary helper flips).

Coordinate convention recap:
- MCP API: millimetres, Y up.
- KiCAD file: millimetres, Y down.
- For PCBs, the page height (from `(paper "A4")`) governs the Y flip;
  defaults to A4 landscape (210mm) if absent.

Board origin: `set_board_outline` places the board with its bottom-left
corner at MCP (`origin_x_mcp`, `origin_y_mcp`) — defaults (10, 10) — so
the board occupies MCP (10..10+w, 10..10+h).
"""

from __future__ import annotations

import copy
import logging
import uuid
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
    round_mm,
)

logger = logging.getLogger("kicad-claude.adapters.pcb_editor")


# --------------------------------------------------------------------------- #
# Page-height detection (same as sch_editor — duplicated to keep them decoupled)
# --------------------------------------------------------------------------- #


def page_height_mm(tree: list) -> float:
    paper = find_child(tree, "paper")
    if paper and len(paper) >= 2 and isinstance(paper[1], str):
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
# Footprint lookup
# --------------------------------------------------------------------------- #


def iter_footprints(tree: list):
    for child in tree[1:]:
        if is_call(child, "footprint"):
            yield child


def _footprint_property(fp: list, name: str) -> str | None:
    for prop in find_children(fp, "property"):
        if len(prop) >= 3 and prop[1] == name and isinstance(prop[2], str):
            return prop[2]
    return None


def get_footprint_reference(fp: list) -> str | None:
    return _footprint_property(fp, "Reference")


def find_footprint_by_reference(tree: list, reference: str) -> list | None:
    for fp in iter_footprints(tree):
        if get_footprint_reference(fp) == reference:
            return fp
    return None


def all_footprint_references(tree: list) -> list[str]:
    return [get_footprint_reference(fp) or "?" for fp in iter_footprints(tree)]


# --------------------------------------------------------------------------- #
# Board outline (Edge.Cuts)
# --------------------------------------------------------------------------- #

EDGE_CUTS_GR_HEADS = {"gr_line", "gr_rect", "gr_arc", "gr_circle", "gr_poly"}


def _is_edge_cuts_node(node: Any) -> bool:
    if not isinstance(node, list):
        return False
    if head_of(node) not in EDGE_CUTS_GR_HEADS:
        return False
    layer = find_child(node, "layer")
    return bool(layer and len(layer) >= 2 and layer[1] == "Edge.Cuts")


def remove_board_outline(tree: list) -> int:
    """Remove every Edge.Cuts graphic. Returns count removed."""
    removed = 0
    i = 1
    while i < len(tree):
        if _is_edge_cuts_node(tree[i]):
            tree.pop(i)
            removed += 1
        else:
            i += 1
    return removed


def set_board_outline(
    tree: list,
    width_mm: float,
    height_mm: float,
    shape: str = "rect",
    origin_x_mcp: float = 10.0,
    origin_y_mcp: float = 10.0,
) -> dict:
    """Replace the Edge.Cuts outline with a `width × height` rectangle.

    The board's bottom-left corner is placed at MCP (`origin_x_mcp`,
    `origin_y_mcp`). Returns a summary including the four corner coordinates.
    """
    if shape not in ("rect", "rounded_rect"):
        raise ValueError(f"shape must be 'rect' or 'rounded_rect' (got {shape!r})")
    if shape == "rounded_rect":
        # Rounded corners need 4 lines + 4 arcs; defer until Phase 6+.
        raise NotImplementedError("rounded_rect outline not yet implemented")

    remove_board_outline(tree)
    page_h = page_height_mm(tree)

    # MCP corners (Y up): bottom-left and top-right.
    bl_mcp = (origin_x_mcp, origin_y_mcp)
    tr_mcp = (origin_x_mcp + width_mm, origin_y_mcp + height_mm)

    bl_k = mcp_to_kicad_xy(*bl_mcp, page_h)  # bottom-left in KiCAD coords (Y down)
    tr_k = mcp_to_kicad_xy(*tr_mcp, page_h)  # top-right in KiCAD coords

    # gr_rect's start/end are diagonal corners; KiCAD doesn't care about the order.
    node = [
        sym("gr_rect"),
        [sym("start"), round_mm(bl_k[0]), round_mm(bl_k[1])],
        [sym("end"), round_mm(tr_k[0]), round_mm(tr_k[1])],
        [sym("stroke"), [sym("width"), 0.15], [sym("type"), sym("solid")]],
        [sym("fill"), sym("no")],
        [sym("layer"), "Edge.Cuts"],
        [sym("uuid"), str(uuid.uuid4())],
    ]
    tree.append(node)
    return {
        "shape": shape,
        "width_mm": width_mm,
        "height_mm": height_mm,
        "bottom_left_mcp": list(bl_mcp),
        "top_right_mcp": list(tr_mcp),
    }


# --------------------------------------------------------------------------- #
# Footprint placement (from a `.kicad_mod` lib def)
# --------------------------------------------------------------------------- #


def fetch_footprint_def(mod_path: Path) -> list:
    """Load a `.kicad_mod` file and return its parsed (footprint ...) tree."""
    text = Path(mod_path).read_text(encoding="utf-8", errors="replace")
    data = sexpdata.loads(text)
    if not is_call(data, "footprint"):
        raise ValueError(f"not a footprint file: {mod_path}")
    return data


def _strip_top_fields(fp: list, names: set[str]) -> list:
    """Return children of `fp` (skipping head + name) with given heads removed."""
    return [c for c in fp[2:] if head_of(c) not in names]


def _build_placed_footprint(
    fp_def: list,
    qualified_lib_id: str,
    reference: str,
    value: str,
    x_k: float,
    y_k: float,
    rotation_deg: int,
    layer: str,
) -> list:
    """Construct a placed footprint instance from a lib (footprint ...) def."""
    placed = copy.deepcopy(fp_def)
    # Drop fields that the placed instance owns directly: layer/at/uuid go in
    # our header. Drop version/generator/generator_version since those describe
    # the source lib, not the placed instance.
    core = _strip_top_fields(
        placed,
        {"version", "generator", "generator_version", "layer", "at", "uuid"},
    )

    # Set Reference / Value properties (preserve their (at), (layer), (effects)).
    for c in core:
        if is_call(c, "property") and len(c) >= 3:
            if c[1] == "Reference":
                c[2] = reference
            elif c[1] == "Value":
                c[2] = value

    header: list[Any] = [
        [sym("layer"), layer],
        [sym("uuid"), str(uuid.uuid4())],
        [sym("at"), round_mm(x_k), round_mm(y_k), rotation_deg],
    ]
    return [sym("footprint"), qualified_lib_id, *header, *core]


def add_footprint(
    tree: list,
    *,
    qualified_lib_id: str,
    reference: str,
    value: str,
    x_mm: float,
    y_mm: float,
    rotation: float = 0,
    layer: str = "F.Cu",
    fp_def_node: list,
) -> list:
    """Place a footprint on the PCB. Returns the new (footprint ...) node."""
    if find_footprint_by_reference(tree, reference) is not None:
        raise ValueError(f"footprint reference {reference!r} already exists")
    if layer not in ("F.Cu", "B.Cu"):
        raise ValueError(f"layer must be 'F.Cu' or 'B.Cu' (got {layer!r})")

    page_h = page_height_mm(tree)
    xk, yk = mcp_to_kicad_xy(x_mm, y_mm, page_h)
    rot = normalize_rotation(rotation)

    placed = _build_placed_footprint(
        fp_def_node,
        qualified_lib_id=qualified_lib_id,
        reference=reference,
        value=value,
        x_k=xk,
        y_k=yk,
        rotation_deg=rot,
        layer=layer,
    )
    tree.append(placed)
    return placed


def remove_footprint(tree: list, reference: str) -> bool:
    for i, child in enumerate(tree):
        if is_call(child, "footprint") and get_footprint_reference(child) == reference:
            tree.pop(i)
            return True
    return False


def move_footprint(
    tree: list,
    reference: str,
    x_mm: float,
    y_mm: float,
    rotation: float | None = None,
    layer: str | None = None,
) -> None:
    fp = find_footprint_by_reference(tree, reference)
    if fp is None:
        raise KeyError(f"no footprint with reference {reference!r}")
    if layer is not None and layer not in ("F.Cu", "B.Cu"):
        raise ValueError(f"layer must be 'F.Cu' or 'B.Cu' (got {layer!r})")

    page_h = page_height_mm(tree)
    xk, yk = mcp_to_kicad_xy(x_mm, y_mm, page_h)
    at = find_child(fp, "at")
    if at is None:
        # Insert one at the right position (after layer/uuid). Fallback: just append.
        at = [sym("at"), round_mm(xk), round_mm(yk)]
        fp.insert(2, at)
    else:
        at[1] = round_mm(xk)
        at[2] = round_mm(yk)
        if rotation is not None:
            rot = normalize_rotation(rotation)
            if len(at) >= 4:
                at[3] = rot
            else:
                at.append(rot)

    if layer is not None:
        layer_node = find_child(fp, "layer")
        if layer_node:
            layer_node[1] = layer


def place_footprints_grid(
    tree: list,
    spacing_mm: float = 10.0,
    columns: int = 5,
    origin_mcp: tuple[float, float] = (15.0, 15.0),
    unplaced_threshold_mm: float = 0.5,
) -> dict:
    """Distribute footprints whose position is within `unplaced_threshold_mm` of (0,0).

    Sorted by reference (R1, R2, …, C1, C2, …) so prefix groups stay contiguous.
    """
    page_h = page_height_mm(tree)

    unplaced: list[list] = []
    for fp in iter_footprints(tree):
        at = find_child(fp, "at")
        if at is None:
            unplaced.append(fp)
            continue
        x = float(at[1]) if len(at) > 1 else 0.0
        y = float(at[2]) if len(at) > 2 else 0.0
        if abs(x) <= unplaced_threshold_mm and abs(y) <= unplaced_threshold_mm:
            unplaced.append(fp)

    unplaced.sort(key=lambda fp: get_footprint_reference(fp) or "?")

    placed = 0
    for i, fp in enumerate(unplaced):
        col = i % columns
        row = i // columns
        x_mcp = origin_mcp[0] + col * spacing_mm
        y_mcp = origin_mcp[1] + row * spacing_mm
        xk, yk = mcp_to_kicad_xy(x_mcp, y_mcp, page_h)
        at = find_child(fp, "at")
        if at is None:
            fp.insert(2, [sym("at"), round_mm(xk), round_mm(yk), 0])
        else:
            at[1] = round_mm(xk)
            at[2] = round_mm(yk)
        placed += 1

    return {"placed": placed, "spacing_mm": spacing_mm, "columns": columns}


# --------------------------------------------------------------------------- #
# Tracks / vias
# --------------------------------------------------------------------------- #


def add_track(
    tree: list,
    x1_mm: float,
    y1_mm: float,
    x2_mm: float,
    y2_mm: float,
    width_mm: float = 0.25,
    layer: str = "F.Cu",
    net: int = 0,
) -> list:
    page_h = page_height_mm(tree)
    x1k, y1k = mcp_to_kicad_xy(x1_mm, y1_mm, page_h)
    x2k, y2k = mcp_to_kicad_xy(x2_mm, y2_mm, page_h)
    node = [
        sym("segment"),
        [sym("start"), round_mm(x1k), round_mm(y1k)],
        [sym("end"), round_mm(x2k), round_mm(y2k)],
        [sym("width"), width_mm],
        [sym("layer"), layer],
        [sym("net"), net],
        [sym("uuid"), str(uuid.uuid4())],
    ]
    tree.append(node)
    return node


def add_via(
    tree: list,
    x_mm: float,
    y_mm: float,
    drill_mm: float = 0.4,
    diameter_mm: float = 0.8,
    net: int = 0,
    layers: tuple[str, str] = ("F.Cu", "B.Cu"),
) -> list:
    page_h = page_height_mm(tree)
    xk, yk = mcp_to_kicad_xy(x_mm, y_mm, page_h)
    node = [
        sym("via"),
        [sym("at"), round_mm(xk), round_mm(yk)],
        [sym("size"), diameter_mm],
        [sym("drill"), drill_mm],
        [sym("layers"), layers[0], layers[1]],
        [sym("net"), net],
        [sym("uuid"), str(uuid.uuid4())],
    ]
    tree.append(node)
    return node


# --------------------------------------------------------------------------- #
# List / export
# --------------------------------------------------------------------------- #


def list_footprints_summary(tree: list) -> list[dict]:
    page_h = page_height_mm(tree)
    out = []
    for fp in iter_footprints(tree):
        ref = get_footprint_reference(fp)
        value = _footprint_property(fp, "Value")
        layer_node = find_child(fp, "layer")
        layer = layer_node[1] if layer_node and len(layer_node) >= 2 else ""
        at = find_child(fp, "at")
        x_k = float(at[1]) if at and len(at) > 1 else 0.0
        y_k = float(at[2]) if at and len(at) > 2 else 0.0
        rot = float(at[3]) if at and len(at) > 3 else 0.0
        # KiCAD -> MCP for display
        x_mcp, y_mcp = x_k, page_h - y_k
        out.append(
            {
                "reference": ref or "?",
                "value": value or "",
                "lib_id": fp[1] if len(fp) > 1 and isinstance(fp[1], str) else "",
                "layer": layer,
                "position_mm": [round_mm(x_mcp), round_mm(y_mcp)],
                "rotation": rot,
            }
        )
    return out
