"""Panelization — duplicate a single PCB into a rows × cols grid.

Approach: parse the source `.kicad_pcb`, walk every translatable element
(footprint, segment, via, zone, gr_line/rect/arc/circle/text), copy it
once per grid cell with a translated position and a reference-designator
suffix `_R{r}C{c}`. Generate a fresh outer Edge.Cuts rectangle for the
whole panel, plus optional mouse-bite holes between cells.

The resulting panel is saved as a new `.kicad_pcb` (default: `panel.kicad_pcb`)
alongside the source. Routing on individual cells is not duplicated —
each cell is a verbatim copy including its tracks/zones.
"""

from __future__ import annotations

import copy
import logging
import math
import uuid
from pathlib import Path
from typing import Any

import sexpdata

from kicad_claude.adapters import pcb_editor as pcb_ed
from kicad_claude.adapters import sch_io
from kicad_claude.adapters.sch_io import find_child, head_of, is_call, sym
from kicad_claude.utils.geometry import round_mm

logger = logging.getLogger("kicad-claude.adapters.panelization")

# Element heads we know how to translate
TRANSLATABLE_HEADS = {
    "footprint", "segment", "via", "zone",
    "gr_line", "gr_rect", "gr_arc", "gr_circle", "gr_poly", "gr_text",
}


# --------------------------------------------------------------------------- #
# Translation primitives
# --------------------------------------------------------------------------- #


def _translate_xy(node: list, dx: float, dy: float) -> None:
    """In-place translate any direct children with structure `(at|start|end|xy x y ...)`."""
    if not isinstance(node, list):
        return
    for child in node[1:]:
        if isinstance(child, list) and len(child) >= 3 and isinstance(child[0], sexpdata.Symbol):
            head = str(child[0])
            if head in ("at", "start", "end", "xy", "center", "mid"):
                if isinstance(child[1], (int, float)):
                    child[1] = round_mm(float(child[1]) + dx)
                if isinstance(child[2], (int, float)):
                    child[2] = round_mm(float(child[2]) + dy)


def _translate_polygon(node: list, dx: float, dy: float) -> None:
    """Walk all (xy ...) inside (polygon (pts ...))."""
    if not isinstance(node, list):
        return
    polygon = find_child(node, "polygon")
    if not polygon:
        return
    pts = find_child(polygon, "pts")
    if not pts:
        return
    for child in pts[1:]:
        if isinstance(child, list) and len(child) >= 3 and is_call(child, "xy"):
            if isinstance(child[1], (int, float)):
                child[1] = round_mm(float(child[1]) + dx)
            if isinstance(child[2], (int, float)):
                child[2] = round_mm(float(child[2]) + dy)


def translate_element(node: list, dx_kicad: float, dy_kicad: float) -> list:
    """Return a deep-copy of `node` translated by (dx, dy) in KiCAD coords.

    Handles footprint (translate `at` only — pads are local), segment/via
    (translate at/start/end), zones (translate polygon points + filled_polygon
    if present), and all gr_* graphics on Edge.Cuts/silk/etc.
    """
    new = copy.deepcopy(node)
    head = head_of(new)
    if head is None:
        return new

    if head == "footprint":
        # Only the parent (at ...) is in board coords; pads are footprint-local.
        _translate_xy(new, dx_kicad, dy_kicad)
        # Refresh uuids on the footprint itself + pads to avoid duplicates
        _refresh_uuids(new)
        return new

    if head == "via":
        _translate_xy(new, dx_kicad, dy_kicad)
        _refresh_uuids(new)
        return new

    if head == "segment":
        _translate_xy(new, dx_kicad, dy_kicad)
        _refresh_uuids(new)
        return new

    if head == "zone":
        # Zone has (polygon (pts (xy ...))) and may have (filled_polygon ...)
        # We only translate the master polygon; filled regions get recomputed
        # when DRC --refill-zones runs. Strip filled_polygon to be safe.
        new[:] = [c for c in new if not is_call(c, "filled_polygon")]
        _translate_polygon(new, dx_kicad, dy_kicad)
        _refresh_uuids(new)
        return new

    if head in ("gr_line", "gr_rect", "gr_arc", "gr_circle"):
        _translate_xy(new, dx_kicad, dy_kicad)
        _refresh_uuids(new)
        return new

    if head == "gr_poly":
        _translate_polygon(new, dx_kicad, dy_kicad)
        _refresh_uuids(new)
        return new

    if head == "gr_text":
        _translate_xy(new, dx_kicad, dy_kicad)
        _refresh_uuids(new)
        return new

    return new


def _refresh_uuids(node: list) -> None:
    """Walk recursively and replace every `(uuid "...")` with a fresh uuid4."""
    if not isinstance(node, list):
        return
    for i, child in enumerate(node):
        if isinstance(child, list):
            if is_call(child, "uuid") and len(child) >= 2:
                child[1] = str(uuid.uuid4())
            else:
                _refresh_uuids(child)


def _suffix_reference(footprint_node: list, suffix: str) -> str | None:
    """Append `suffix` to the footprint's Reference property. Returns the new ref."""
    for prop in sch_io.find_children(footprint_node, "property"):
        if len(prop) >= 3 and prop[1] == "Reference" and isinstance(prop[2], str):
            new_ref = f"{prop[2]}{suffix}"
            prop[2] = new_ref
            return new_ref
    return None


# --------------------------------------------------------------------------- #
# Panel building
# --------------------------------------------------------------------------- #


def _outline_bounds_kicad(tree: list) -> tuple[float, float, float, float] | None:
    """Return (min_x, min_y, max_x, max_y) in KiCAD coords of the gr_rect outline.

    Returns None if no rectangular Edge.Cuts is found.
    """
    poly = pcb_ed.get_board_outline_polygon_kicad(tree)
    if poly is None:
        return None
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    return min(xs), min(ys), max(xs), max(ys)


def _make_outline_rect(min_x: float, min_y: float, max_x: float, max_y: float) -> list:
    return [
        sym("gr_rect"),
        [sym("start"), round_mm(min_x), round_mm(min_y)],
        [sym("end"), round_mm(max_x), round_mm(max_y)],
        [sym("stroke"), [sym("width"), 0.15], [sym("type"), sym("solid")]],
        [sym("fill"), sym("no")],
        [sym("layer"), "Edge.Cuts"],
        [sym("uuid"), str(uuid.uuid4())],
    ]


def _make_mouse_bite_hole(x: float, y: float, drill_mm: float = 0.5) -> list:
    """Generate a circular Edge.Cuts hole at (x, y).

    KiCAD treats Edge.Cuts circles as board cutouts — these become drilled
    perforations on the panel that snap apart by hand.
    """
    return [
        sym("gr_circle"),
        [sym("center"), round_mm(x), round_mm(y)],
        [sym("end"), round_mm(x + drill_mm / 2), round_mm(y)],
        [sym("stroke"), [sym("width"), 0.05], [sym("type"), sym("solid")]],
        [sym("fill"), sym("no")],
        [sym("layer"), "Edge.Cuts"],
        [sym("uuid"), str(uuid.uuid4())],
    ]


def panelize_grid(
    source_tree: list,
    *,
    rows: int,
    cols: int,
    h_gap_mm: float = 2.0,
    v_gap_mm: float = 2.0,
    mouse_bites: bool = True,
    mouse_bite_drill_mm: float = 0.5,
    mouse_bite_spacing_mm: float = 1.0,
) -> dict:
    """Build a fresh tree that is `rows × cols` copies of `source_tree`.

    Returns a dict with `tree` (the new panel s-expression list), `outline_size_mm`,
    and `cell_count`.
    """
    if rows < 1 or cols < 1:
        raise ValueError("rows and cols must be ≥ 1")
    bounds = _outline_bounds_kicad(source_tree)
    if bounds is None:
        raise RuntimeError(
            "source PCB has no rectangular Edge.Cuts outline — "
            "run set_board_outline first"
        )
    ox_min, oy_min, ox_max, oy_max = bounds
    cell_w = ox_max - ox_min
    cell_h = oy_max - oy_min

    # Start with a fresh tree carrying header bits + lib_symbols-equivalent
    # (none for PCB) + layers + setup from the source.
    panel: list[Any] = [sym("kicad_pcb")]
    for child in source_tree[1:]:
        h = head_of(child)
        if h in ("version", "generator", "generator_version", "general", "paper",
                 "title_block", "layers", "setup"):
            panel.append(copy.deepcopy(child))
    # Net 0 declaration (default unconnected) — required
    panel.append([sym("net"), 0, ""])

    # Carry over net declarations from source so segments retain correct net indices
    seen_net_indices = {0}
    for net_node in sch_io.find_children(source_tree, "net"):
        if len(net_node) >= 2 and isinstance(net_node[1], int):
            idx = int(net_node[1])
            if idx in seen_net_indices:
                continue
            seen_net_indices.add(idx)
            panel.append(copy.deepcopy(net_node))

    # Translate every element type once per (r, c) cell.
    # Source elements EXCEPT Edge.Cuts (we'll generate a new outer outline).
    source_elements = [
        c for c in source_tree[1:]
        if head_of(c) in TRANSLATABLE_HEADS
        and not _is_edge_cuts_only(c)  # skip the original outline
    ]

    cell_count = 0
    for r in range(rows):
        for c in range(cols):
            # Cell origin offset (KiCAD coords, Y increases downward)
            offset_x = c * (cell_w + h_gap_mm)
            offset_y = r * (cell_h + v_gap_mm)
            suffix = f"_R{r + 1}C{c + 1}"
            for elem in source_elements:
                new_elem = translate_element(elem, offset_x, offset_y)
                if head_of(new_elem) == "footprint":
                    _suffix_reference(new_elem, suffix)
                panel.append(new_elem)
            cell_count += 1

    # Outer outline: encompasses the full panel
    panel_w = cols * cell_w + (cols - 1) * h_gap_mm
    panel_h = rows * cell_h + (rows - 1) * v_gap_mm
    panel.append(_make_outline_rect(ox_min, oy_min, ox_min + panel_w, oy_min + panel_h))

    # Mouse bites between boards (interior gaps only)
    if mouse_bites:
        # Vertical dividers: (cols - 1) of them, each a row of holes from top to bottom
        for c in range(cols - 1):
            x_center = ox_min + (c + 1) * cell_w + (c + 0.5) * h_gap_mm
            y_start = oy_min
            y_end = oy_min + panel_h
            n = max(2, int((y_end - y_start) / mouse_bite_spacing_mm) + 1)
            for i in range(n):
                y = y_start + i * (y_end - y_start) / max(1, n - 1)
                panel.append(_make_mouse_bite_hole(x_center, y, mouse_bite_drill_mm))

        # Horizontal dividers: (rows - 1)
        for r in range(rows - 1):
            y_center = oy_min + (r + 1) * cell_h + (r + 0.5) * v_gap_mm
            x_start = ox_min
            x_end = ox_min + panel_w
            n = max(2, int((x_end - x_start) / mouse_bite_spacing_mm) + 1)
            for i in range(n):
                x = x_start + i * (x_end - x_start) / max(1, n - 1)
                panel.append(_make_mouse_bite_hole(x, y_center, mouse_bite_drill_mm))

    return {
        "tree": panel,
        "outline_size_mm": [round_mm(panel_w), round_mm(panel_h)],
        "cell_size_mm": [round_mm(cell_w), round_mm(cell_h)],
        "cell_count": cell_count,
    }


def _is_edge_cuts_only(node: list) -> bool:
    """True if node is a graphics primitive that lives only on Edge.Cuts."""
    h = head_of(node)
    if h not in ("gr_line", "gr_rect", "gr_arc", "gr_circle", "gr_poly"):
        return False
    layer = find_child(node, "layer")
    return bool(layer and len(layer) >= 2 and layer[1] == "Edge.Cuts")
