"""Read a symbol out of a `.kicad_sym`, ready to embed in a schematic.

Why this exists: KiCad 10's stock libraries define about half of their symbols
as `(extends "Base")` stubs that carry only properties. A schematic's
`lib_symbols` block must hold a *flat* symbol, so copying the stub verbatim
yields a part with no pins (and an ERC `lib_symbol_mismatch`). `load_symbol_def`
resolves the extends chain into one self-contained symbol.

Also exposes small read helpers for multi-unit symbols (`unit_numbers`,
`pins_of_unit`) used by placement and pin lookup.
"""

from __future__ import annotations

import copy
from pathlib import Path

import sexpdata

from kicad_claude.adapters.sch_io import find_child, find_children, head_of, is_call

_MAX_EXTENDS_DEPTH = 8  # real libraries chain 1-2 deep; guards against cycles


def _read_lib(lib_path: Path) -> list:
    data = sexpdata.loads(Path(lib_path).read_text(encoding="utf-8", errors="replace"))
    if not is_call(data, "kicad_symbol_lib"):
        raise ValueError(f"not a kicad_symbol_lib: {lib_path}")
    return data


def _find_symbol(lib_tree: list, name: str) -> list:
    for child in lib_tree[1:]:
        if is_call(child, "symbol") and len(child) >= 2 and child[1] == name:
            return child
    raise KeyError(f"symbol {name!r} not found in library")


def _unit_suffix(sub_name: str, base_name: str) -> str | None:
    """`R_1_1` with base `R` -> `_1_1`; None if it is not a unit of that base."""
    if sub_name.startswith(base_name + "_"):
        tail = sub_name[len(base_name):]
        parts = tail.split("_")
        if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
            return tail
    return None


def _merge_properties(flat: list, derived: list) -> None:
    """Overlay the derived symbol's properties onto the flattened base."""
    for prop in find_children(derived, "property"):
        existing = next(
            (p for p in find_children(flat, "property") if p[1] == prop[1]), None
        )
        if existing is not None:
            existing[:] = copy.deepcopy(prop)
        else:
            props = find_children(flat, "property")
            idx = flat.index(props[-1]) + 1 if props else len(flat)
            flat.insert(idx, copy.deepcopy(prop))


def _resolve(lib_tree: list, name: str, depth: int) -> list:
    node = _find_symbol(lib_tree, name)
    ext = find_child(node, "extends")
    if ext is None:
        return copy.deepcopy(node)
    if depth >= _MAX_EXTENDS_DEPTH:
        raise ValueError(f"extends chain too deep (cycle?) at {name!r}")
    base_name = ext[1]
    flat = _resolve(lib_tree, base_name, depth + 1)
    for child in flat[2:]:
        if is_call(child, "symbol") and isinstance(child[1], str):
            suffix = _unit_suffix(child[1], base_name)
            if suffix is not None:
                child[1] = name + suffix
    flat[1] = name
    _merge_properties(flat, node)
    return flat


def load_symbol_def(lib_path: Path, symbol_name: str) -> list:
    """Return a flat deep copy of `symbol_name` with any `extends` resolved."""
    return _resolve(_read_lib(lib_path), symbol_name, 0)


def _unit_of(sub_name: str) -> int | None:
    parts = sub_name.rsplit("_", 2)
    if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
        return int(parts[1])
    return None


def unit_numbers(symbol_def: list) -> list[int]:
    """Real unit numbers (1..N) of a lib symbol; unit 0 (shared) is excluded."""
    units = set()
    for child in symbol_def[2:]:
        if is_call(child, "symbol") and isinstance(child[1], str):
            u = _unit_of(child[1])
            if u:
                units.add(u)
    return sorted(units) or [1]


def pins_of_unit(symbol_def: list, unit: int) -> list[tuple[list, int]]:
    """Pins drawn when `unit` is placed: its own plus the shared unit-0 ones.

    Returns (pin_node, unit_number) pairs; shared pins report unit 0. Body
    styles other than the first (De Morgan alternates) are skipped.
    """
    out: list[tuple[list, int]] = []
    for child in symbol_def[2:]:
        h = head_of(child)
        if h == "pin":
            out.append((child, 0))
        elif h == "symbol" and isinstance(child[1], str):
            parts = child[1].rsplit("_", 2)
            if len(parts) != 3 or not (parts[1].isdigit() and parts[2].isdigit()):
                continue
            u, style = int(parts[1]), int(parts[2])
            if style > 1 or u not in (0, unit):
                continue
            out += [(p, u) for p in find_children(child, "pin")]
    return out
