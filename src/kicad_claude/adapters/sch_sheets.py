"""Sheet-level helpers: paper size now, sheet pins in a later change.

Why paper needs care: MCP coordinates are Y-up and are flipped around the
page height on the way into the file (`sch_editor.page_height_mm`). Changing
the paper of a sheet that already holds items would silently move all of them
relative to MCP coordinates, so `set_paper` only accepts an empty sheet.
"""

from __future__ import annotations

from kicad_claude.adapters.sch_io import find_child, head_of, sym

# Landscape sizes in mm as (width, height); KiCad's own list of ISO/US sheets.
PAPER_SIZES_MM: dict[str, tuple[float, float]] = {
    "A5": (210.0, 148.0),
    "A4": (297.0, 210.0),
    "A3": (420.0, 297.0),
    "A2": (594.0, 420.0),
    "A1": (841.0, 594.0),
    "A0": (1189.0, 841.0),
    "USLetter": (279.4, 215.9),
    "USLegal": (355.6, 215.9),
    "USLedger": (431.8, 279.4),
}

# Top-level nodes that carry drawn content (everything but header bookkeeping).
_CONTENT_HEADS = frozenset({
    "symbol", "wire", "bus", "bus_entry", "junction", "no_connect", "label",
    "global_label", "hierarchical_label", "text", "text_box", "sheet",
    "polyline", "rectangle", "circle", "arc", "image",
})


def has_content(tree: list) -> bool:
    """True if the sheet holds anything placed (lib_symbols alone doesn't count)."""
    return any(head_of(c) in _CONTENT_HEADS for c in tree[1:])


def _user_paper(width_mm: float | None, height_mm: float | None) -> tuple[float, float]:
    if width_mm is None or height_mm is None:
        raise ValueError("paper 'User' needs width_mm and height_mm")
    if not (50 <= width_mm <= 3000 and 50 <= height_mm <= 3000):
        raise ValueError("User paper sides must be between 50 and 3000 mm")
    return float(width_mm), float(height_mm)


def set_paper(
    tree: list,
    size: str,
    width_mm: float | None = None,
    height_mm: float | None = None,
) -> str:
    """Set the sheet's paper size; refuses if the sheet already has content.

    `size` is a named paper, or "User" with explicit `width_mm` / `height_mm`
    (KiCad stores `(paper "User" w h)`).
    """
    if size != "User" and size not in PAPER_SIZES_MM:
        raise ValueError(f"paper must be one of {[*PAPER_SIZES_MM, 'User']} (got {size!r})")
    node = [sym("paper"), size]
    if size == "User":
        node += list(_user_paper(width_mm, height_mm))
    if has_content(tree):
        raise ValueError(
            "sheet already has content: changing paper would shift it against "
            "the Y-up coordinates. Set the paper before placing anything."
        )
    paper = find_child(tree, "paper")
    if paper is None:
        idx = next((i + 1 for i, c in enumerate(tree) if head_of(c) == "uuid"), 1)
        tree.insert(idx, node)
    else:
        paper[:] = node
    return size


def page_size_mm(tree: list) -> tuple[float, float]:
    """(width, height) of the sheet in mm; A4 when no paper is declared."""
    paper = find_child(tree, "paper")
    if paper and len(paper) >= 4 and paper[1] == "User":
        return float(paper[2]), float(paper[3])
    name = paper[1] if paper and len(paper) >= 2 else "A4"
    return PAPER_SIZES_MM.get(name, PAPER_SIZES_MM["A4"])


# --------------------------------------------------------------------------- #
# Sheet pins (the parent-side twin of a child's hierarchical labels)
# --------------------------------------------------------------------------- #

SHEET_PIN_PITCH_MM = 2.54  # one pin per grid pair so labels don't touch
_RIGHT_EDGE_SHAPES = frozenset({"output"})  # KiCad convention: outputs leave on the right


def child_interface(child_tree: list) -> list[tuple[str, str]]:
    """(name, shape) of every hierarchical label in a child sheet, first wins."""
    seen: dict[str, str] = {}
    for node in child_tree[1:]:
        if head_of(node) == "hierarchical_label":
            shape = find_child(node, "shape")
            seen.setdefault(node[1], str(shape[1]) if shape else "bidirectional")
    return list(seen.items())


def _pins_of(sheet_node: list) -> list[list]:
    return [c for c in sheet_node[1:] if head_of(c) == "pin"]


def sheet_pin_positions(sheet_node: list, page_h: float) -> list[dict]:
    """Existing sheet pins as {name, shape, position_mm (MCP), orientation}."""
    out = []
    for pin in _pins_of(sheet_node):
        at = find_child(pin, "at")
        out.append({
            "name": pin[1],
            "shape": str(pin[2]),
            "position_mm": [round(float(at[1]), 4), round(page_h - float(at[2]), 4)],
            "angle": int(at[3]),
        })
    return out


def _edge_for(shape: str) -> str:
    return "right" if shape in _RIGHT_EDGE_SHAPES else "left"


def sync_sheet_pins(sheet_node: list, interface: list[tuple[str, str]], page_h: float) -> dict:
    """Add a sheet pin for every child label that has none; grow the sheet to fit.

    Inputs/bidirectional/passive go on the left edge, outputs on the right,
    stacked at 2.54 mm. Existing pins are kept as they are; pins whose label
    vanished from the child are reported as `stale`, not deleted.
    """
    from kicad_claude.adapters import sch_editor as ed  # local: avoid a module cycle

    have = {p[1] for p in _pins_of(sheet_node)}
    at, size = find_child(sheet_node, "at"), find_child(sheet_node, "size")
    left, top = float(at[1]), float(at[2])
    width = float(size[1])
    counts = {"left": sum(1 for p in _pins_of(sheet_node) if int(find_child(p, "at")[3]) == 180),
              "right": sum(1 for p in _pins_of(sheet_node) if int(find_child(p, "at")[3]) == 0)}
    added = []
    for name, shape in interface:
        if name in have:
            continue
        edge = _edge_for(shape)
        counts[edge] += 1
        y_k = round(top + SHEET_PIN_PITCH_MM * counts[edge], 4)
        x_k = left if edge == "left" else round(left + width, 4)
        ed.add_sheet_pin(
            sheet_node, pin_name=name, shape=shape, x_mm=x_k, y_mm=page_h - y_k,
            page_h=page_h, orientation="left" if edge == "left" else "right",
        )
        added.append(name)
    rows = max(counts.values(), default=0)
    needed = SHEET_PIN_PITCH_MM * (rows + 1)
    if needed > float(size[2]):
        size[2] = needed
    labels = {n for n, _ in interface}
    return {"added": added, "stale": sorted(have - labels), "height_mm": float(size[2])}
