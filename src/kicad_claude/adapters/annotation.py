"""Auto-annotate schematic reference designators (`R?` → `R1`, `R2`, …).

Algorithm:
1. Walk every instance symbol in the given tree.
2. Group by reference prefix (the part before any digit, e.g. "R" for "R?").
3. Find existing max number per prefix.
4. Assign sequential numbers to symbols whose ref ends with "?", sorted by
   (Y descending, X ascending) — so top-down, left-right reading order.

For multi-sheet (hierarchical) projects, run this once per sheet from the
caller side. Numbering continues across sheets if you preserve `counters`
between calls.
"""

from __future__ import annotations

import re
from typing import Iterable

from kicad_claude.adapters import sch_editor as ed
from kicad_claude.adapters import sch_io

_REF_PATTERN = re.compile(r"([A-Za-z#_]+)(\d+)$")


def find_unannotated_symbols(tree: list) -> list:
    """Return symbols whose Reference ends with `?`."""
    out = []
    for s in ed.iter_instance_symbols(tree):
        ref = ed.get_symbol_property(s, "Reference") or ""
        if ref.endswith("?"):
            out.append(s)
    return out


def existing_max_per_prefix(tree: list) -> dict[str, int]:
    """Return the highest reference number for each prefix already in use.

    Empty if no symbols are annotated yet.
    """
    out: dict[str, int] = {}
    for s in ed.iter_instance_symbols(tree):
        ref = ed.get_symbol_property(s, "Reference") or ""
        m = _REF_PATTERN.fullmatch(ref)
        if m:
            prefix, num = m.group(1), int(m.group(2))
            out[prefix] = max(out.get(prefix, 0), num)
    return out


def _symbol_position(s: list) -> tuple[float, float]:
    """Return KiCAD-coords (x, y) for sorting. (0, 0) if missing."""
    at = sch_io.find_child(s, "at")
    if not at or len(at) < 3:
        return (0.0, 0.0)
    return (float(at[1]), float(at[2]))


def annotate_tree(
    tree: list,
    *,
    counters: dict[str, int] | None = None,
    sort_by_position: bool = True,
) -> list[dict]:
    """Annotate `?`-suffixed symbols in `tree`. Mutates the tree in place.

    `counters` (dict prefix→max_used) lets the caller carry the running
    high-water mark across multiple sheets. If not provided, starts from
    the existing references inside `tree`.

    Returns a list of `{old, new}` dicts for each annotation made.
    """
    if counters is None:
        counters = existing_max_per_prefix(tree)
    targets = find_unannotated_symbols(tree)
    if sort_by_position:
        # KiCAD Y is down, so smaller Y = top of page. Sort top-down (asc Y), then left-right (asc X).
        targets.sort(key=lambda s: _symbol_position(s)[::-1])

    assignments: list[dict] = []
    for s in targets:
        ref = ed.get_symbol_property(s, "Reference") or ""
        prefix = ref[:-1]  # strip trailing '?'
        if not prefix:
            # Just '?' alone — skip; nothing meaningful we can do.
            continue
        next_num = counters.get(prefix, 0) + 1
        counters[prefix] = next_num
        new_ref = f"{prefix}{next_num}"

        # Update the Reference property AND the (instances ... (path ... (reference ...)))
        ed.set_symbol_property(s, "Reference", new_ref)
        _update_instance_reference(s, new_ref)

        assignments.append({"old": ref, "new": new_ref})
    return assignments


def _update_instance_reference(sym_node: list, new_ref: str) -> None:
    """Update the `(reference "...")` inside every `(instances ... (path ...))`."""
    instances = sch_io.find_child(sym_node, "instances")
    if not instances:
        return
    for project in sch_io.find_children(instances, "project"):
        for path in sch_io.find_children(project, "path"):
            for ref_node in sch_io.find_children(path, "reference"):
                if len(ref_node) >= 2:
                    ref_node[1] = new_ref


# --------------------------------------------------------------------------- #
# Multi-sheet helper
# --------------------------------------------------------------------------- #


def annotate_sheets(sheet_paths: Iterable, sort_by_position: bool = True) -> dict:
    """Annotate every sheet in turn, sharing one counter across all of them.

    `sheet_paths` is an iterable of Path objects to .kicad_sch files. Each
    file is parsed, mutated, and written back. Returns per-sheet stats and
    overall totals.
    """
    counters: dict[str, int] = {}
    sheet_results: list[dict] = []

    # First pass: collect existing high-water marks across all sheets so we
    # don't accidentally re-use a number from a sibling sheet.
    paths = list(sheet_paths)
    for p in paths:
        tree = sch_io.parse_file(p)
        for prefix, n in existing_max_per_prefix(tree).items():
            counters[prefix] = max(counters.get(prefix, 0), n)

    # Second pass: annotate each sheet, mutating in place.
    total = 0
    for p in paths:
        tree = sch_io.parse_file(p)
        assignments = annotate_tree(
            tree, counters=counters, sort_by_position=sort_by_position
        )
        if assignments:
            sch_io.write_file(p, tree)
        total += len(assignments)
        sheet_results.append(
            {"sheet": str(p), "assignments": assignments, "count": len(assignments)}
        )

    return {
        "total_assignments": total,
        "sheets": sheet_results,
        "high_water": counters,
    }
