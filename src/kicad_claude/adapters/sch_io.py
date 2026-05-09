"""Read and write KiCAD `.kicad_sch` / `.kicad_pcb` s-expression files.

Parses with `sexpdata`. Writes with a custom pretty-printer that mimics
KiCAD's tab-indented multi-line layout. KiCAD will accept any well-formed
s-expression and reformats on next save, but our pretty output makes diffs
and debugging easier.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import sexpdata

# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


def parse_file(path: Path) -> list:
    """Parse a KiCAD s-expression file. Returns the top-level list."""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    return sexpdata.loads(text)


# --------------------------------------------------------------------------- #
# Pretty-printer
# --------------------------------------------------------------------------- #


def dumps(node: Any, indent: int = 0) -> str:
    """Serialize a parsed s-expression tree in KiCAD-like tab-indented format.

    Rules (matching KiCAD eeschema/pcbnew output):
      - Atom-only lists are inline:               (at 39.37 29.21 0)
      - Lists with sublists put head + leading
        atoms on the first line, then each
        sublist child on its own indented line.
      - Tab indentation, one tab per level.
      - Closing `)` on its own line at parent indent.
    """
    if not isinstance(node, list):
        return _atom(node)
    if not node:
        return "()"

    # Find first child (after the head) that is itself a list.
    first_list_idx: int | None = None
    for i in range(1, len(node)):
        if isinstance(node[i], list):
            first_list_idx = i
            break

    if first_list_idx is None:
        # All atoms — single line.
        return "(" + " ".join(_atom(c) for c in node) + ")"

    leading = " ".join(_atom(c) for c in node[:first_list_idx])
    rest = node[first_list_idx:]
    inner_pad = "\t" * (indent + 1)
    outer_pad = "\t" * indent

    lines = ["(" + leading]
    for child in rest:
        if isinstance(child, list):
            lines.append(inner_pad + dumps(child, indent + 1))
        else:
            lines.append(inner_pad + _atom(child))
    lines.append(outer_pad + ")")
    return "\n".join(lines)


def write_file(path: Path, tree: list) -> None:
    """Write `tree` to `path` (KiCAD-style formatting + trailing newline)."""
    Path(path).write_text(dumps(tree) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# Atom serialization
# --------------------------------------------------------------------------- #


def _atom(x: Any) -> str:
    if isinstance(x, sexpdata.Symbol):
        return str(x)
    if isinstance(x, bool):  # must precede int — bool is an int subclass
        return "yes" if x else "no"
    if isinstance(x, str):
        return '"' + _escape(x) + '"'
    if isinstance(x, int):
        return str(x)
    if isinstance(x, float):
        return _format_float(x)
    return str(x)


def _escape(s: str) -> str:
    """Backslash-escape `\\` and `"` for KiCAD string literals."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _format_float(x: float) -> str:
    """Format a float the way KiCAD does: trim trailing zeros, keep no `e`-notation."""
    if math.isnan(x) or math.isinf(x):
        return repr(x)
    if x == 0.0:
        return "0"
    if x == int(x) and abs(x) < 1e15:
        # Integer-valued floats: keep as integer-style "5", not "5.0".
        # KiCAD writes pin angles as `0`, `90` (no decimal).
        return str(int(x))
    # Use Python's default float repr; trim noise.
    s = f"{x:.6f}".rstrip("0").rstrip(".")
    return s if s else "0"


# --------------------------------------------------------------------------- #
# Tree-walking helpers
# --------------------------------------------------------------------------- #


def is_call(node: Any, head: str) -> bool:
    """True if `node` is `(head ...)` — a list whose first elem is the symbol `head`."""
    return (
        isinstance(node, list)
        and len(node) > 0
        and isinstance(node[0], sexpdata.Symbol)
        and str(node[0]) == head
    )


def head_of(node: Any) -> str | None:
    if isinstance(node, list) and node and isinstance(node[0], sexpdata.Symbol):
        return str(node[0])
    return None


def find_child(node: list, head: str) -> list | None:
    """Return the first direct child of `node` that is `(head ...)`, or None."""
    for c in node[1:] if node else []:
        if is_call(c, head):
            return c
    return None


def find_children(node: list, head: str) -> list:
    """Return all direct children of `node` that are `(head ...)`."""
    return [c for c in (node[1:] if node else []) if is_call(c, head)]


def sym(name: str) -> sexpdata.Symbol:
    """Convenience: wrap a Python string as a sexpdata.Symbol."""
    return sexpdata.Symbol(name)
