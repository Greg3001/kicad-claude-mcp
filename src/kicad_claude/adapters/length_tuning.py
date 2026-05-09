"""Length-tuning geometry — generate serpentine traces between two points.

Pure-Python; no KiCAD dependencies. The PCB editor uses these waypoints to
emit a chain of `(segment ...)` nodes.

Single-side triangular meander pattern:

        /\\      /\\      /\\
   ____/  \\____/  \\____/  \\____
   ↑ pre-straight  ↑ bumps  ↑ post-straight

Each bump goes amplitude perpendicular over base_width along the axis.
Per-bump trace length: 2·hypot(base_width/2, amplitude).
Per-bump axis length: base_width.
Extra per bump: 2·hypot(base_width/2, amplitude) − base_width.
"""

from __future__ import annotations

import math


def straight_length(start: tuple[float, float], end: tuple[float, float]) -> float:
    return math.hypot(end[0] - start[0], end[1] - start[1])


def generate_meander(
    start: tuple[float, float],
    end: tuple[float, float],
    target_length_mm: float,
    *,
    amplitude_mm: float = 1.5,
    side: int = 1,
    base_width_mm: float | None = None,
) -> list[tuple[float, float]]:
    """Build a sequence of waypoints from start to end totalling `target_length_mm`.

    `amplitude_mm` is the perpendicular height of each bump (default 1.5 mm).
    `side`: +1 for "above" the line (CCW perpendicular), -1 for "below".
    `base_width_mm`: width of each bump along the axis (default = 2·amplitude).

    Returns a list of (x, y) waypoints starting with `start` and ending with
    `end`. Connecting them with line segments produces the meander.

    Raises ValueError if target_length is shorter than the straight distance,
    or if the bumps don't fit within the available straight distance.
    """
    if amplitude_mm <= 0:
        raise ValueError(f"amplitude must be > 0 (got {amplitude_mm})")
    if side not in (-1, 1):
        raise ValueError(f"side must be -1 or +1 (got {side})")

    sx, sy = start
    ex, ey = end
    straight = straight_length(start, end)
    if straight < 1e-9:
        raise ValueError("start and end coincide")
    if target_length_mm < straight - 1e-6:
        raise ValueError(
            f"target_length {target_length_mm:.3f} mm is shorter than the "
            f"straight distance {straight:.3f} mm"
        )

    delta = target_length_mm - straight
    if delta < 1e-9:
        return [start, end]

    # Default base_width = amplitude → sharp triangular peaks, packs ~1.24× extra
    # per base unit. With base = 2·A you only get ~0.41× extra per base, which
    # often won't fit a long target in a short straight distance.
    base_width = base_width_mm if base_width_mm is not None else amplitude_mm
    half_base = base_width / 2

    # Each triangular bump: peak at axis midpoint, perpendicular by amplitude.
    extra_per_bump = 2 * math.hypot(half_base, amplitude_mm) - base_width
    if extra_per_bump <= 0:
        raise ValueError(
            f"amplitude {amplitude_mm} too small relative to base_width {base_width}; "
            "increase amplitude"
        )

    n_bumps = max(1, math.ceil(delta / extra_per_bump))
    region = n_bumps * base_width
    if region > straight:
        # Can't fit — caller should increase amplitude or shorten target.
        raise ValueError(
            f"meander region needs {region:.3f} mm but only {straight:.3f} mm is "
            f"available; increase amplitude (currently {amplitude_mm}) or split "
            "the meander into multiple segments."
        )

    # Recompute exact extra: with `n_bumps` chosen integer, we may overshoot
    # the target. Solve for the amplitude that achieves the target exactly.
    # 2·hypot(half_base, A) − base_width = delta / n_bumps
    target_extra_per_bump = delta / n_bumps
    # 2·sqrt(b² + A²) = b·base_width + delta/n_bumps + base_width  → A = sqrt(((b·b + extra_per_bump_target)/2)² − b²)
    half_target = (target_extra_per_bump + base_width) / 2
    A_eff_squared = half_target * half_target - half_base * half_base
    if A_eff_squared <= 0:
        # Fall back to original amplitude
        A_eff = amplitude_mm
    else:
        A_eff = math.sqrt(A_eff_squared)

    # Unit axis vector and perpendicular
    ux, uy = (ex - sx) / straight, (ey - sy) / straight
    px, py = -uy * side, ux * side

    pre = (straight - region) / 2
    pts: list[tuple[float, float]] = [start]
    cx, cy = sx + ux * pre, sy + uy * pre
    pts.append((cx, cy))
    for _ in range(n_bumps):
        # Peak: half_base along axis from current baseline, then perpendicular A_eff
        peak_x = cx + ux * half_base + px * A_eff
        peak_y = cy + uy * half_base + py * A_eff
        pts.append((peak_x, peak_y))
        # Back to baseline at full base_width along axis
        cx += ux * base_width
        cy += uy * base_width
        pts.append((cx, cy))
    pts.append(end)
    return pts


def waypoints_total_length(points: list[tuple[float, float]]) -> float:
    """Sum the lengths of consecutive (x, y) point pairs."""
    total = 0.0
    for i in range(len(points) - 1):
        total += straight_length(points[i], points[i + 1])
    return total
