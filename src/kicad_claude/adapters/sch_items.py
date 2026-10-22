"""Small schematic items the editor lacked, plus a one-pass batch applier.

Why this exists: text notes and the DNP flag had no tool at all, and drawing a
sheet item by item costs one parse + one write of the whole file per call.
`apply_items` validates a list of drawing ops, then applies them all to one
parsed tree so the caller pays the file I/O once.
"""

from __future__ import annotations

import uuid

from kicad_claude.adapters import sch_connect
from kicad_claude.adapters import sch_editor as ed
from kicad_claude.adapters.sch_io import find_child, sym
from kicad_claude.utils.geometry import mcp_to_kicad_xy, round_mm

MAX_BATCH_ITEMS = 5000  # bounds one call; a 480-pin sheet needs ~1500


def add_text(tree: list, text: str, x_mm: float, y_mm: float, size_mm: float = 1.27) -> list:
    """Append a free text note (may contain newlines) at an MCP-coordinate point."""
    if not text:
        raise ValueError("text must be non-empty")
    xk, yk = mcp_to_kicad_xy(x_mm, y_mm, ed.page_height_mm(tree))
    node = [
        sym("text"),
        text,
        [sym("exclude_from_sim"), sym("no")],
        [sym("at"), round_mm(xk), round_mm(yk), 0],
        [
            sym("effects"),
            [sym("font"), [sym("size"), size_mm, size_mm]],
            [sym("justify"), sym("left"), sym("bottom")],
        ],
        [sym("uuid"), str(uuid.uuid4())],
    ]
    tree.append(node)
    return node


def set_dnp(tree: list, reference: str, dnp: bool) -> int:
    """Flip `(dnp ...)` on every placed unit of `reference`; returns units changed."""
    nodes = ed.find_symbols_by_reference(tree, reference)
    if not nodes:
        raise KeyError(f"no symbol with reference {reference!r}")
    for node in nodes:
        flag = find_child(node, "dnp")
        if flag is None:
            node.insert(node.index(find_child(node, "uuid")), [sym("dnp"), sym("yes" if dnp else "no")])
        else:
            flag[:] = [sym("dnp"), sym("yes" if dnp else "no")]
    return len(nodes)


def _need(op: dict, *keys: str) -> list:
    missing = [k for k in keys if k not in op]
    if missing:
        raise ValueError(f"op {op.get('op')!r} needs {missing}")
    return [op[k] for k in keys]


def _op_wire(tree, op):
    ed.add_wire(tree, *_need(op, "x1", "y1", "x2", "y2"))


def _op_junction(tree, op):
    ed.add_junction(tree, *_need(op, "x", "y"))


def _op_no_connect(tree, op):
    ed.add_no_connect(tree, *_need(op, "x", "y"))


def _op_label(tree, op):
    net, x, y = _need(op, "net", "x", "y")
    ed.add_label(tree, net, x, y, op.get("orientation", "right"))


def _op_global_label(tree, op):
    net, x, y = _need(op, "net", "x", "y")
    sch_connect.add_global_label(tree, net, x, y, op.get("shape", "bidirectional"),
                                 op.get("orientation", "right"))


def _op_hierarchical_label(tree, op):
    net, x, y = _need(op, "net", "x", "y")
    ed.add_hierarchical_label(tree, net_name=net, x_mm=x, y_mm=y,
                              shape=op.get("shape", "bidirectional"),
                              orientation=op.get("orientation", "right"))


def _op_text(tree, op):
    text, x, y = _need(op, "text", "x", "y")
    add_text(tree, text, x, y, op.get("size", 1.27))


OPS = {
    "wire": _op_wire,
    "junction": _op_junction,
    "no_connect": _op_no_connect,
    "label": _op_label,
    "global_label": _op_global_label,
    "hierarchical_label": _op_hierarchical_label,
    "text": _op_text,
}


def apply_items(tree: list, items: list[dict]) -> dict[str, int]:
    """Apply drawing ops in order; raises on the first bad one.

    The caller writes the tree only after this returns, so a bad op anywhere
    in the list leaves the file untouched.
    """
    if len(items) > MAX_BATCH_ITEMS:
        raise ValueError(f"at most {MAX_BATCH_ITEMS} items per call (got {len(items)})")
    counts: dict[str, int] = {}
    for i, op in enumerate(items):
        name = op.get("op")
        if name not in OPS:
            raise ValueError(f"item {i}: op must be one of {sorted(OPS)} (got {name!r})")
        OPS[name](tree, op)
        counts[name] = counts.get(name, 0) + 1
    return counts
