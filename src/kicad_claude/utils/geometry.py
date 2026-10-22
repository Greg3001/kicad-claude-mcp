"""Coordinate-system helpers.

The MCP API exposes:
- millimetres (float)
- Y axis pointing UP (so "+5V at top" means high Y)

KiCAD `.kicad_sch` files use:
- millimetres (float, sometimes nanometres internally — handled by file format)
- Y axis pointing DOWN (origin at top-left of page)

We translate at the boundary in `mcp_to_kicad_xy` / `kicad_to_mcp_xy`. The
default page height assumes A4 landscape (the KiCAD schematic default), but
callers may override it after reading `(paper ...)`.

Schematics flip around `grid_aligned_height(page_h)` rather than the raw page
height: A4's 210 mm is not a multiple of KiCAD's 1.27 mm connection grid, so
flipping around it would push every on-grid MCP point off-grid in the file.
"""

from __future__ import annotations

import math

# A4 landscape — KiCAD's default schematic page (x_max=297, y_max=210 mm).
DEFAULT_PAGE_HEIGHT_MM = 210.0

# KiCAD's schematic connection grid (50 mil). Flip heights are aligned to twice
# this (100 mil) so both 1.27 and 2.54 mm MCP grids survive the Y flip.
SCH_GRID_MM = 1.27


def grid_aligned_height(page_height_mm: float, grid_mm: float = 2 * SCH_GRID_MM) -> float:
    """Largest multiple of `grid_mm` not exceeding the page height (A4: 208.28)."""
    return round(math.floor(page_height_mm / grid_mm + 1e-9) * grid_mm, 4)


def mcp_to_kicad_xy(
    x_mm: float, y_mm: float, page_height_mm: float = DEFAULT_PAGE_HEIGHT_MM
) -> tuple[float, float]:
    """Translate a point from MCP coords (Y up) to KiCAD file coords (Y down)."""
    return float(x_mm), float(page_height_mm) - float(y_mm)


def kicad_to_mcp_xy(
    x_mm: float, y_mm: float, page_height_mm: float = DEFAULT_PAGE_HEIGHT_MM
) -> tuple[float, float]:
    """Inverse of mcp_to_kicad_xy. The transform is its own inverse."""
    return float(x_mm), float(page_height_mm) - float(y_mm)


def normalize_rotation(deg: float) -> int:
    """Snap a rotation to {0, 90, 180, 270}. Phase 3 only supports right angles."""
    r = int(round(float(deg))) % 360
    if r not in (0, 90, 180, 270):
        raise ValueError(
            f"rotation must be a multiple of 90° (got {deg}); free angles unsupported"
        )
    return r


def rotate_xy(x: float, y: float, deg: float) -> tuple[float, float]:
    """Rotate a vector around the origin by `deg` (counter-clockwise, math sense).

    KiCAD's rotation in the schematic file is also CCW around the symbol origin.
    """
    rad = math.radians(deg)
    cos_r = math.cos(rad)
    sin_r = math.sin(rad)
    return x * cos_r - y * sin_r, x * sin_r + y * cos_r


def round_mm(value: float, digits: int = 4) -> float:
    """KiCAD uses 6-decimal precision internally; 4 is plenty for placements."""
    return round(float(value), digits)


def off_grid(points: list[tuple[float, float]], grid_mm: float = SCH_GRID_MM) -> list[tuple[float, float]]:
    """The points whose x or y is not a multiple of the connection grid."""
    def on(v: float) -> bool:
        return math.isclose(v / grid_mm, round(v / grid_mm), abs_tol=1e-3)

    return [p for p in points if not (on(p[0]) and on(p[1]))]


def grid_warning(points: list[tuple[float, float]], what: str) -> list[str]:
    """A one-element warning list when any point is off the 1.27 mm grid, else []."""
    bad = off_grid(points)
    if not bad:
        return []
    return [
        f"{len(bad)} of {len(points)} {what} are off KiCad's 1.27 mm connection grid "
        f"(first: {bad[0]}); ERC will report endpoint_off_grid and wires will not "
        f"connect. Use multiples of 1.27 (symbol origins on 2.54 if pins sit on 1.27 offsets)."
    ]
