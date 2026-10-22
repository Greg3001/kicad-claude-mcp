"""Put every schematic part on the PCB in one pass.

Why this exists: `update_pcb_from_schematic` only assigns nets to footprints
already on the board and reports the rest as `missing_in_pcb`. For a 265-part
carrier that meant 265 `add_footprint` calls, each re-parsing the board.
`plan_placements` reads the kicadxml netlist KiCad writes and lays the missing
parts out on a coarse grid (a starting point for the layout engineer, not a
placement); `place_all` applies them with one board parse and one cache of the
parsed `.kicad_mod` files.
"""

from __future__ import annotations

import copy
import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from kicad_claude.adapters import pcb_editor as ed

DEFAULT_PITCH_MM = 15.0  # clears most 0603..SOIC parts; connectors will overlap
DEFAULT_COLUMNS = 20  # 20 columns x 15 mm = 300 mm wide, a plausible board width


@dataclass(frozen=True)
class Part:
    reference: str
    value: str
    footprint: str  # "Lib:Name", "" when the schematic part has none


def netlist_parts(xml_path: Path) -> list[Part]:
    """Parts of a kicadxml netlist, in reference order (natural: R2 before R10)."""
    root = ET.parse(xml_path).getroot()
    parts = [
        Part(c.get("ref") or "", c.findtext("value") or "", (c.findtext("footprint") or "").strip())
        for c in root.findall("components/comp")
    ]
    return sorted((p for p in parts if p.reference), key=lambda p: _natural(p.reference))


def _natural(ref: str) -> tuple:
    head = ref.rstrip("0123456789")
    tail = ref[len(head):]
    return (head, int(tail) if tail else -1)


def grid_position(index: int, columns: int, pitch_mm: float, origin: tuple[float, float]) -> tuple[float, float]:
    """MCP coordinates of the `index`-th grid cell (row-major, rows going down)."""
    row, col = divmod(index, columns)
    return origin[0] + col * pitch_mm, origin[1] - row * pitch_mm


def plan_placements(
    parts: list[Part],
    on_board: set[str],
    *,
    columns: int = DEFAULT_COLUMNS,
    pitch_mm: float = DEFAULT_PITCH_MM,
    origin: tuple[float, float] = (10.0, 280.0),
) -> dict:
    """Split parts into those to place (with positions) and those to skip."""
    if columns < 1 or pitch_mm <= 0:
        raise ValueError("columns must be >= 1 and pitch_mm > 0")
    todo = [p for p in parts if p.reference not in on_board and p.footprint]
    return {
        "place": [(p, *grid_position(i, columns, pitch_mm, origin)) for i, p in enumerate(todo)],
        "no_footprint": [p.reference for p in parts if not p.footprint and p.reference not in on_board],
        "already_on_board": sorted(p.reference for p in parts if p.reference in on_board),
        "rows": math.ceil(len(todo) / columns) if todo else 0,
    }


def place_all(tree: list, plan: dict, resolve) -> dict:
    """Add each planned footprint to `tree`; `resolve(lib_id) -> Path` finds the .kicad_mod.

    A footprint that cannot be resolved is reported, not fatal: the rest of the
    board still gets populated.
    """
    cache: dict[Path, list] = {}
    placed, unresolved = [], []
    for part, x, y in plan["place"]:
        try:
            path = resolve(part.footprint)
        except (KeyError, FileNotFoundError, ValueError) as e:
            unresolved.append({"reference": part.reference, "footprint": part.footprint, "reason": str(e)[:120]})
            continue
        if path not in cache:
            cache[path] = ed.fetch_footprint_def(path)
        ed.add_footprint(
            tree, qualified_lib_id=part.footprint, reference=part.reference,
            value=part.value, x_mm=x, y_mm=y, fp_def_node=copy.deepcopy(cache[path]),
        )
        placed.append(part.reference)
    return {"placed": placed, "unresolved": unresolved}
