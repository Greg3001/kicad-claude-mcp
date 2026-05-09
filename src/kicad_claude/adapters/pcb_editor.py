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

import math
import re

from kicad_claude.adapters import length_tuning, pcb_layers, sch_io
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
# Layer-count reconfiguration
# --------------------------------------------------------------------------- #


def get_copper_layer_count(tree: list) -> int:
    """Count the signal layers (copper) in the active PCB."""
    layers = find_child(tree, "layers")
    if not layers:
        return 0
    n = 0
    for row in layers[1:]:
        if isinstance(row, list) and len(row) >= 3:
            kind = row[2]
            if isinstance(kind, type(sym("x"))) and str(kind) == "signal":
                n += 1
    return n


def get_copper_layer_names(tree: list) -> list[str]:
    """Names of signal copper layers in file-order: F.Cu, In*.Cu..., B.Cu."""
    layers = find_child(tree, "layers")
    if not layers:
        return []
    names: list[str] = []
    for row in layers[1:]:
        if isinstance(row, list) and len(row) >= 3:
            kind = row[2]
            if isinstance(kind, type(sym("x"))) and str(kind) == "signal":
                if isinstance(row[1], str):
                    names.append(row[1])
    return names


def set_copper_layer_count(tree: list, n: int) -> dict:
    """Replace the `(layers ...)` block and the `(setup (stackup ...) ...)`
    sub-block to reflect `n` copper layers.

    Existing tracks/vias/footprints on layers that disappear are NOT migrated;
    they keep their layer name in the file but those layers won't exist, and
    KiCAD's DRC will complain. Set the layer count BEFORE adding tracks.
    """
    n = int(n)
    if n < 2 or n > pcb_layers.MAX_COPPER_LAYERS or n % 2:
        raise ValueError(
            f"copper layer count must be even and 2-{pcb_layers.MAX_COPPER_LAYERS} "
            f"(got {n})"
        )

    # Replace (layers ...)
    new_layers = pcb_layers.build_layers_block(n)
    for i, child in enumerate(tree):
        if is_call(child, "layers"):
            tree[i] = new_layers
            break
    else:
        # Insert near the top (after paper / general)
        insert_at = 1
        for i, child in enumerate(tree[1:], start=1):
            h = head_of(child)
            if h in ("paper", "general"):
                insert_at = i + 1
        tree.insert(insert_at, new_layers)

    # Replace (stackup ...) inside (setup ...)
    setup = find_child(tree, "setup")
    new_stackup = pcb_layers.build_stackup_block(n)
    if setup is None:
        # Setup block is required; create a minimal one with just the stackup.
        setup = [sym("setup"), new_stackup]
        tree.append(setup)
    else:
        replaced = False
        for i, child in enumerate(setup):
            if is_call(child, "stackup"):
                setup[i] = new_stackup
                replaced = True
                break
        if not replaced:
            setup.insert(1, new_stackup)

    return {
        "copper_layers": n,
        "layer_names": pcb_layers.copper_layer_names(n),
    }


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
# Net inspection / diff pairs / trace length
# --------------------------------------------------------------------------- #


def list_nets(tree: list) -> list[dict]:
    """Return [{index, name}] for every (net N "name") declaration in the PCB."""
    out: list[dict] = []
    for n in find_children(tree, "net"):
        if len(n) >= 3 and isinstance(n[1], int):
            out.append({"index": int(n[1]), "name": str(n[2])})
    return out


def find_net_index(tree: list, net_name: str) -> int | None:
    for n in find_children(tree, "net"):
        if len(n) >= 3 and n[2] == net_name:
            return int(n[1])
    return None


# Suffix conventions for diff pair members. Order matters: more-specific first.
_DIFF_PATTERNS: list[tuple[str, str, str]] = [
    # (positive_suffix, negative_suffix, conjunction explainer)
    ("_P", "_N", "_P/_N"),
    ("+", "-", "+/−"),
    ("DP", "DM", "DP/DM (USB-style)"),
    ("_p", "_n", "_p/_n (lowercase)"),
]


def find_diff_pair_candidates(tree: list) -> list[dict]:
    """Detect pairs of nets that look like differential pairs by name.

    Returns a list of dicts with `base_name`, `p`, `n`, and `convention`.
    Skips nets that don't have a partner present.
    """
    names = {n["name"] for n in list_nets(tree) if n["name"]}
    pairs: list[dict] = []
    seen: set[tuple[str, str]] = set()

    for name in sorted(names):
        for p_suf, n_suf, label in _DIFF_PATTERNS:
            if name.endswith(p_suf) and len(name) > len(p_suf):
                base = name[: -len(p_suf)]
                partner = base + n_suf
                if partner in names:
                    key = tuple(sorted((name, partner)))
                    if key in seen:
                        continue
                    seen.add(key)
                    # Strip trailing separator from the displayed base name
                    # ("USB_DP" -> "USB", not "USB_").
                    display_base = base.rstrip("_-.")
                    pairs.append({
                        "base_name": display_base,
                        "p": name,
                        "n": partner,
                        "convention": label,
                    })
                    break  # only emit each net once
    return pairs


def compute_trace_length(tree: list, net_name: str) -> dict:
    """Sum lengths of every (segment ...) on the named net.

    Returns mm total plus per-layer breakdown. Multi-layer traces are
    counted across all layers; vias add zero length.
    """
    idx = find_net_index(tree, net_name)
    if idx is None:
        raise KeyError(f"unknown net {net_name!r}")
    total = 0.0
    by_layer: dict[str, float] = {}
    seg_count = 0
    for seg in find_children(tree, "segment"):
        net = find_child(seg, "net")
        if not net or int(net[1]) != idx:
            continue
        start = find_child(seg, "start")
        end = find_child(seg, "end")
        if not (start and end and len(start) >= 3 and len(end) >= 3):
            continue
        seg_len = math.hypot(
            float(end[1]) - float(start[1]),
            float(end[2]) - float(start[2]),
        )
        total += seg_len
        layer_node = find_child(seg, "layer")
        layer_name = layer_node[1] if layer_node and len(layer_node) >= 2 else ""
        by_layer[layer_name] = by_layer.get(layer_name, 0.0) + seg_len
        seg_count += 1
    return {
        "net": net_name,
        "total_mm": round_mm(total),
        "segment_count": seg_count,
        "by_layer_mm": {k: round_mm(v) for k, v in by_layer.items()},
    }


def add_meander_segments(
    tree: list,
    *,
    start_mm: tuple[float, float],
    end_mm: tuple[float, float],
    target_length_mm: float,
    amplitude_mm: float = 1.5,
    side: str = "up",
    width_mm: float = 0.25,
    layer: str = "F.Cu",
    net_name: str | None = None,
    base_width_mm: float | None = None,
) -> list[list]:
    """Generate a meander between two points and emit (segment ...) nodes.

    `side`: "up" / "down" (perpendicular direction). `net_name` resolves to
    the net index; pass None for net 0 (default unconnected). Returns the
    list of new segment nodes appended to the tree.
    """
    side_map = {"up": 1, "down": -1, "left": 1, "right": -1}
    if side not in side_map:
        raise ValueError(f"side must be 'up' or 'down' (got {side!r})")

    waypoints = length_tuning.generate_meander(
        start_mm, end_mm, target_length_mm,
        amplitude_mm=amplitude_mm,
        side=side_map[side],
        base_width_mm=base_width_mm,
    )
    net_idx = 0
    if net_name:
        idx = find_net_index(tree, net_name)
        if idx is None:
            raise KeyError(f"net {net_name!r} not found; declare it first or omit net_name")
        net_idx = idx

    page_h = page_height_mm(tree)
    new_segments: list[list] = []
    for i in range(len(waypoints) - 1):
        x1m, y1m = waypoints[i]
        x2m, y2m = waypoints[i + 1]
        x1k, y1k = mcp_to_kicad_xy(x1m, y1m, page_h)
        x2k, y2k = mcp_to_kicad_xy(x2m, y2m, page_h)
        node = [
            sym("segment"),
            [sym("start"), round_mm(x1k), round_mm(y1k)],
            [sym("end"), round_mm(x2k), round_mm(y2k)],
            [sym("width"), round_mm(width_mm)],
            [sym("layer"), layer],
            [sym("net"), net_idx],
            [sym("uuid"), _uuid()],
        ]
        tree.append(node)
        new_segments.append(node)
    return new_segments


def _uuid() -> str:
    import uuid as _u
    return str(_u.uuid4())


# --------------------------------------------------------------------------- #
# Copper zones (pours)
# --------------------------------------------------------------------------- #


def get_board_outline_polygon_kicad(tree: list) -> list[tuple[float, float]] | None:
    """Extract the board outline as KiCAD-coord (x, y) points.

    Currently supports rectangular outlines (`gr_rect` on Edge.Cuts), since
    that's what `set_board_outline` produces. Returns None if no rectangular
    Edge.Cuts is found.
    """
    for node in find_children(tree, "gr_rect"):
        layer = find_child(node, "layer")
        if not layer or len(layer) < 2 or layer[1] != "Edge.Cuts":
            continue
        start = find_child(node, "start")
        end = find_child(node, "end")
        if not (start and end and len(start) >= 3 and len(end) >= 3):
            continue
        x1, y1 = float(start[1]), float(start[2])
        x2, y2 = float(end[1]), float(end[2])
        # Counter-clockwise from bottom-left in KiCAD's Y-down system, which is
        # actually clockwise visually. KiCAD doesn't care about winding order.
        return [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]
    return None


def add_zone(
    tree: list,
    *,
    net_name: str,
    layer: str,
    polygon_mcp: list[tuple[float, float]],
    fill_clearance_mm: float = 0.5,
    min_thickness_mm: float = 0.25,
    thermal_gap_mm: float = 0.5,
    thermal_bridge_mm: float = 0.5,
    name: str = "",
    priority: int = 0,
) -> list:
    """Add a filled copper zone for `net_name` on `layer` covering `polygon_mcp`.

    Polygon points are in MCP coordinates (Y up). The zone is declared as
    `(fill yes)` so KiCAD's DRC will compute the filled regions on demand
    (refill via `kicad-cli pcb drc --refill-zones` or in the GUI).

    `layer` may be a single copper layer ("F.Cu", "B.Cu", "In1.Cu", …) or
    a glob like "*.Cu" for every copper layer.
    """
    if not polygon_mcp or len(polygon_mcp) < 3:
        raise ValueError("polygon needs at least 3 points")

    # Resolve net index by name
    nets = find_children(tree, "net")
    net_idx = 0
    for n in nets:
        if (
            len(n) >= 3
            and isinstance(n[2], str)
            and n[2] == net_name
        ):
            net_idx = int(n[1])
            break
    else:
        # Allocate a new (net N "name") at the top level
        if net_name:
            next_idx = max(
                (int(n[1]) for n in nets if len(n) >= 2),
                default=-1,
            ) + 1
            tree.insert(_first_child_index(tree, "footprint", default=len(tree)),
                        [sym("net"), next_idx, net_name])
            net_idx = next_idx

    page_h = page_height_mm(tree)
    pts_kicad = [mcp_to_kicad_xy(x, y, page_h) for (x, y) in polygon_mcp]
    pts_block: list[Any] = [sym("pts")]
    for x, y in pts_kicad:
        pts_block.append([sym("xy"), round_mm(x), round_mm(y)])

    # Decide layer node: `layer` for single, `layers` for multiple
    if "*" in layer:
        layer_node = [sym("layers"), layer]
    else:
        layer_node = [sym("layer"), layer]

    zone_node = [
        sym("zone"),
        [sym("net"), net_idx],
        [sym("net_name"), net_name],
        layer_node,
        [sym("uuid"), str(uuid.uuid4())],
        [sym("name"), name or f"{net_name}_{layer}"],
        [sym("hatch"), sym("edge"), 0.5],
        [sym("priority"), priority],
        [sym("connect_pads"), [sym("clearance"), round_mm(fill_clearance_mm)]],
        [sym("min_thickness"), round_mm(min_thickness_mm)],
        [sym("filled_areas_thickness"), sym("no")],
        [
            sym("fill"),
            sym("yes"),
            [sym("thermal_gap"), round_mm(thermal_gap_mm)],
            [sym("thermal_bridge_width"), round_mm(thermal_bridge_mm)],
            [sym("smoothing"), sym("none")],
            [sym("radius"), 1.0],
            [sym("island_removal_mode"), 0],
            [sym("island_area_min"), 10.0],
        ],
        [sym("polygon"), pts_block],
    ]
    tree.append(zone_node)
    return zone_node


def add_ground_plane(
    tree: list,
    *,
    layer: str = "B.Cu",
    net_name: str = "GND",
    fill_clearance_mm: float = 0.5,
) -> list:
    """Convenience: pour a ground plane on `layer` covering the whole board."""
    poly_kicad = get_board_outline_polygon_kicad(tree)
    if poly_kicad is None:
        raise RuntimeError(
            "no board outline found; call set_board_outline first so the zone "
            "knows what area to fill."
        )
    page_h = page_height_mm(tree)
    poly_mcp = [(x, page_h - y) for (x, y) in poly_kicad]
    return add_zone(
        tree,
        net_name=net_name,
        layer=layer,
        polygon_mcp=poly_mcp,
        fill_clearance_mm=fill_clearance_mm,
        name=f"{net_name}_{layer}",
    )


def _first_child_index(tree: list, head: str, default: int) -> int:
    for i, c in enumerate(tree[1:], start=1):
        if is_call(c, head):
            return i
    return default


# --------------------------------------------------------------------------- #
# Silk / fab text
# --------------------------------------------------------------------------- #


def add_silk_text(
    tree: list,
    *,
    text: str,
    x_mm: float,
    y_mm: float,
    layer: str = "F.SilkS",
    size_mm: float = 1.0,
    rotation: float = 0,
    thickness_mm: float | None = None,
) -> list:
    """Add a `(gr_text ...)` to the PCB, default on F.SilkS.

    Common layers: F.SilkS, B.SilkS, F.Fab, B.Fab, F.Cu, B.Cu (text on copper).
    """
    if thickness_mm is None:
        thickness_mm = round_mm(size_mm * 0.15)
    rot = normalize_rotation(rotation)
    page_h = page_height_mm(tree)
    xk, yk = mcp_to_kicad_xy(x_mm, y_mm, page_h)
    node = [
        sym("gr_text"),
        text,
        [sym("at"), round_mm(xk), round_mm(yk), rot],
        [sym("layer"), layer],
        [sym("uuid"), str(uuid.uuid4())],
        [
            sym("effects"),
            [
                sym("font"),
                [sym("size"), round_mm(size_mm), round_mm(size_mm)],
                [sym("thickness"), round_mm(thickness_mm)],
            ],
        ],
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
