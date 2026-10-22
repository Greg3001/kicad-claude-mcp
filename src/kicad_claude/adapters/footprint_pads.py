"""Pad generators for the two package families that dominate TI/AMD parts.

Why this exists: `create_footprint` takes a pad list, so a 48-pin QFN or a
44-pin HTSSOP with a thermal pad meant hand-computing 49 coordinates. These
return pad dicts for `create_footprint`. Footprint coordinates: mm, origin at
the package centre, Y pointing DOWN (KiCad's footprint frame). Pin 1 is top-left
and numbering runs counter-clockwise as seen from above, the JEDEC convention.

Pure functions, no I/O.
"""

from __future__ import annotations

MAX_PINS = 400  # far above any real package; stops a typo from writing a huge file


def _pad(number, x: float, y: float, size_x: float, size_y: float, shape: str = "rect") -> dict:
    return {"number": str(number), "type": "smd", "shape": shape,
            "x_mm": round(x, 4), "y_mm": round(y, 4),
            "size_x_mm": round(size_x, 4), "size_y_mm": round(size_y, 4)}


def _exposed_pad(number, width: float | None, height: float | None) -> list[dict]:
    if width is None and height is None:
        return []
    if width is None or height is None or width <= 0 or height <= 0:
        raise ValueError("an exposed pad needs both ep_width_mm and ep_height_mm (> 0)")
    return [_pad(number, 0.0, 0.0, width, height)]


def _check_positive(**values: float) -> None:
    for name, v in values.items():
        if v is None or v <= 0:
            raise ValueError(f"{name} must be > 0 (got {v})")


def dual_row_pads(
    pin_count: int,
    pitch_mm: float,
    row_span_mm: float,
    pad_length_mm: float,
    pad_width_mm: float,
    ep_width_mm: float | None = None,
    ep_height_mm: float | None = None,
    ep_number: str | None = None,
) -> list[dict]:
    """SOIC / TSSOP / HTSSOP / SO-PowerPAD: two rows facing each other.

    `row_span_mm` is the centre-to-centre distance between the rows; the pads
    are `pad_length_mm` long across the row direction and `pad_width_mm` wide
    along it. The exposed pad is optional; it is numbered `ep_number`, by default
    the next number after the last pin (what KiCad's stock QFN/SOIC-EP footprints do).
    """
    if pin_count < 2 or pin_count % 2 or pin_count > MAX_PINS:
        raise ValueError("pin_count must be an even number from 2 to 400")
    _check_positive(pitch_mm=pitch_mm, row_span_mm=row_span_mm,
                    pad_length_mm=pad_length_mm, pad_width_mm=pad_width_mm)
    half = pin_count // 2
    y0 = -(half - 1) * pitch_mm / 2
    pads = []
    for i in range(half):  # left column, top to bottom
        pads.append(_pad(i + 1, -row_span_mm / 2, y0 + i * pitch_mm, pad_length_mm, pad_width_mm))
    for i in range(half):  # right column, bottom to top
        pads.append(_pad(half + i + 1, row_span_mm / 2, -y0 - i * pitch_mm, pad_length_mm, pad_width_mm))
    return pads + _exposed_pad(ep_number or str(pin_count + 1), ep_width_mm, ep_height_mm)


def qfn_pads(
    pins_per_side: int,
    pitch_mm: float,
    span_mm: float,
    pad_length_mm: float,
    pad_width_mm: float,
    ep_width_mm: float | None = None,
    ep_height_mm: float | None = None,
    ep_number: str | None = None,
    pins_top_bottom: int | None = None,
    span_y_mm: float | None = None,
    end_pad_length_mm: float | None = None,
) -> list[dict]:
    """QFN / VQFN / WQFN / LGA four-sided footprint, square or rectangular.

    `pins_per_side` pads on the left and right sides, `pins_top_bottom` on the
    top and bottom ends (default: same, a square part). `span_mm` is the
    pad-centre distance between the left and right columns, `span_y_mm`
    between the top and bottom rows (default: `span_mm`). Pads on the ends can
    be shorter or longer than on the sides (`end_pad_length_mm`). Numbering:
    left side top to bottom, bottom left to right, right side bottom to top,
    top right to left. The exposed pad is numbered `ep_number`, by default
    the next number after the last pin.
    """
    ends = pins_per_side if pins_top_bottom is None else pins_top_bottom
    if pins_per_side < 1 or ends < 1 or 2 * (pins_per_side + ends) > MAX_PINS:
        raise ValueError("pins_per_side and pins_top_bottom must be >= 1, at most 400 pins in all")
    span_y = span_mm if span_y_mm is None else span_y_mm
    end_len = pad_length_mm if end_pad_length_mm is None else end_pad_length_mm
    _check_positive(pitch_mm=pitch_mm, span_mm=span_mm, span_y_mm=span_y,
                    pad_length_mm=pad_length_mm, pad_width_mm=pad_width_mm,
                    end_pad_length_mm=end_len)
    ex, ey = span_mm / 2, span_y / 2
    y_first = -(pins_per_side - 1) * pitch_mm / 2
    x_first = -(ends - 1) * pitch_mm / 2
    pads, n = [], 0
    for k in range(pins_per_side):  # left, top -> bottom
        n += 1
        pads.append(_pad(n, -ex, y_first + k * pitch_mm, pad_length_mm, pad_width_mm))
    for k in range(ends):  # bottom, left -> right
        n += 1
        pads.append(_pad(n, x_first + k * pitch_mm, ey, pad_width_mm, end_len))
    for k in range(pins_per_side):  # right, bottom -> top
        n += 1
        pads.append(_pad(n, ex, -y_first - k * pitch_mm, pad_length_mm, pad_width_mm))
    for k in range(ends):  # top, right -> left
        n += 1
        pads.append(_pad(n, -x_first - k * pitch_mm, -ey, pad_width_mm, end_len))
    return pads + _exposed_pad(ep_number or str(n + 1), ep_width_mm, ep_height_mm)
