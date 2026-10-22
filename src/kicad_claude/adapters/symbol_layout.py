"""Automatic pin layout for generated multi-unit symbols.

Why this exists: a SOM or FPGA connector can have hundreds of pins. Asking
the caller for an (x, y) per pin is impractical, so pins that carry only a
number, a name and a type are stacked on the left/right of a body sized to fit
their names. Pure functions: no I/O, no KiCad.

Geometry (library coordinates, Y up, mm): pin tips sit on the 1.27 mm
connection grid; rows are 2.54 mm apart so wires fan out without touching.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

PIN_PITCH_MM = 2.54  # one row per pin, the standard 100 mil pitch
PIN_LENGTH_MM = 2.54  # stub between the connection point and the body edge
NAME_CHAR_WIDTH_MM = 1.0  # 1.27 mm font renders ~1 mm per character
MIN_HALF_WIDTH_MM = 5.08  # keeps small units from collapsing into a sliver
BODY_PAD_MM = 2.54  # clearance between the longest left/right names
SIDES = ("left", "right")


@dataclass(frozen=True)
class PlacedPin:
    """A pin resolved to library coordinates."""

    spec: dict
    x_mm: float
    y_mm: float
    angle_deg: int  # direction from the connection point INTO the body (KiCad)


@dataclass(frozen=True)
class UnitLayout:
    pins: list[PlacedPin]
    half_width_mm: float
    half_height_mm: float


def is_explicit(pin: dict) -> bool:
    return pin.get("x_mm") is not None and pin.get("y_mm") is not None


def _assign_sides(pins: list[dict]) -> dict[str, list[dict]]:
    """Honour `side`; split the rest in input order, left half first."""
    out: dict[str, list[dict]] = {"left": [], "right": []}
    free = []
    for pin in pins:
        side = pin.get("side")
        if side is None:
            free.append(pin)
        elif side in SIDES:
            out[side].append(pin)
        else:
            raise ValueError(f"pin {pin.get('number')!r}: side must be one of {SIDES}")
    split = max(math.ceil(len(free) / 2) - len(out["left"]) // 2, 0)
    out["left"] += free[:split]
    out["right"] += free[split:]
    return out


def _round_up(value: float, step: float) -> float:
    return round(math.ceil(value / step - 1e-9) * step, 4)


def _name_width(pins: list[dict]) -> float:
    return max((len(str(p.get("name", "~"))) for p in pins), default=0) * NAME_CHAR_WIDTH_MM


def layout_unit(pins: list[dict]) -> UnitLayout:
    """Stack `pins` (no explicit x/y) on the two vertical sides of a body."""
    sides = _assign_sides(pins)
    rows = max(len(sides["left"]), len(sides["right"]), 1)
    needed = _name_width(sides["left"]) + _name_width(sides["right"]) + BODY_PAD_MM
    half_w = max(MIN_HALF_WIDTH_MM, _round_up(needed / 2, PIN_PITCH_MM))
    half_h = (rows + 1) * PIN_PITCH_MM / 2
    placed: list[PlacedPin] = []
    for side, sign, angle in (("left", -1, 0), ("right", 1, 180)):
        for i, pin in enumerate(sides[side]):
            y = round(half_h - PIN_PITCH_MM * (i + 1), 4)
            x = round(sign * (half_w + PIN_LENGTH_MM), 4)
            placed.append(PlacedPin(pin, x, y, angle))
    return UnitLayout(placed, half_w, half_h)
