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
"""

from __future__ import annotations

import math

# A4 landscape — KiCAD's default schematic page (x_max=297, y_max=210 mm).
DEFAULT_PAGE_HEIGHT_MM = 210.0


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
