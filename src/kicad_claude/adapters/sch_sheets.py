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


def set_paper(tree: list, size: str) -> str:
    """Set the sheet's paper size; refuses if the sheet already has content."""
    if size not in PAPER_SIZES_MM:
        raise ValueError(f"paper must be one of {list(PAPER_SIZES_MM)} (got {size!r})")
    if has_content(tree):
        raise ValueError(
            "sheet already has content: changing paper would shift it against "
            "the Y-up coordinates. Set the paper before placing anything."
        )
    paper = find_child(tree, "paper")
    if paper is None:
        idx = next((i + 1 for i, c in enumerate(tree) if head_of(c) == "uuid"), 1)
        tree.insert(idx, [sym("paper"), size])
    else:
        paper[:] = [sym("paper"), size]
    return size


def page_size_mm(tree: list) -> tuple[float, float]:
    """(width, height) of the sheet in mm; A4 when no paper is declared."""
    paper = find_child(tree, "paper")
    name = paper[1] if paper and len(paper) >= 2 else "A4"
    return PAPER_SIZES_MM.get(name, PAPER_SIZES_MM["A4"])
