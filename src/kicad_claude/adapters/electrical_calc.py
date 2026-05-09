"""Closed-form electrical formulas for PCB design.

Pure-Python; no PCB tree dependencies. Functions take simple numeric
inputs and return numbers (or dicts for richer outputs).

Sources:
- IPC-2141A: characteristic impedance for microstrip / stripline
- Polar Instruments: differential pair coupling correction
- IPC-2152: external/internal trace current capacity
"""

from __future__ import annotations

import math
from typing import Literal


# --------------------------------------------------------------------------- #
# Trace impedance
# --------------------------------------------------------------------------- #


def microstrip_impedance(
    width_mm: float,
    height_mm: float,
    er: float = 4.5,
    thickness_mm: float = 0.035,
) -> float:
    """Single-ended microstrip characteristic impedance Z₀ in ohms (IPC-2141A).

    Formula: Z₀ = (87 / sqrt(εᵣ + 1.41)) × ln(5.98h / (0.8w + t))

    Valid range: 0.1 ≤ w/h ≤ 2.0. Outside this band the formula is less
    accurate (typical error <5% inside the band; up to ~15% near the edges).

    Args:
        width_mm:    trace width
        height_mm:   distance from trace bottom to reference plane (dielectric thickness)
        er:          relative permittivity (FR4 ≈ 4.5; Rogers RO4350B = 3.66)
        thickness_mm: copper thickness (1 oz = 0.035 mm; 2 oz = 0.070 mm)

    Returns:
        Z₀ in ohms.
    """
    if width_mm <= 0 or height_mm <= 0:
        raise ValueError("width and height must be positive")
    if er <= 0:
        raise ValueError("er must be positive")
    return (87.0 / math.sqrt(er + 1.41)) * math.log(
        5.98 * height_mm / (0.8 * width_mm + thickness_mm)
    )


def stripline_impedance(
    width_mm: float,
    dielectric_total_mm: float,
    er: float = 4.5,
    thickness_mm: float = 0.035,
) -> float:
    """Stripline (between two ground planes) Z₀ in ohms (IPC-2141A).

    Formula: Z₀ = (60 / sqrt(εᵣ)) × ln(4b / (0.67π × (0.8w + t)))

    `dielectric_total_mm` (b) is the total distance between the upper and
    lower reference planes; the trace is centered.

    Args: see `microstrip_impedance`.
    """
    if width_mm <= 0 or dielectric_total_mm <= 0:
        raise ValueError("width and dielectric must be positive")
    if er <= 0:
        raise ValueError("er must be positive")
    return (60.0 / math.sqrt(er)) * math.log(
        4 * dielectric_total_mm / (0.67 * math.pi * (0.8 * width_mm + thickness_mm))
    )


def differential_microstrip_impedance(
    width_mm: float,
    gap_mm: float,
    height_mm: float,
    er: float = 4.5,
    thickness_mm: float = 0.035,
) -> float:
    """Differential microstrip impedance Z_diff in ohms (Polar approximation).

    Z_diff = 2 × Z₀_single × (1 − 0.48 × exp(−0.96 × s/h))

    Common targets: USB 2.0 ≈ 90 Ω, HDMI/Ethernet ≈ 100 Ω, MIPI/LVDS ≈ 100 Ω.
    """
    z0 = microstrip_impedance(width_mm, height_mm, er=er, thickness_mm=thickness_mm)
    return 2 * z0 * (1 - 0.48 * math.exp(-0.96 * gap_mm / height_mm))


def coplanar_waveguide_impedance(
    width_mm: float,
    gap_mm: float,
    height_mm: float,
    er: float = 4.5,
    thickness_mm: float = 0.035,
) -> float:
    """CPW with ground (CPWG) impedance Z₀ in ohms (Wadell / Wheeler approximation).

    Coplanar waveguide on a substrate with a ground plane below.
    `width_mm` (w) = signal trace width.
    `gap_mm` (g)   = gap between signal trace and adjacent ground pour.
    `height_mm` (h)= dielectric thickness.

    The full closed-form involves complete elliptic integrals; we use the
    Wheeler approximation good to ~5% for h > w (i.e. typical RF cases).
    """
    if width_mm <= 0 or gap_mm <= 0 or height_mm <= 0:
        raise ValueError("width, gap, height must be positive")

    a = width_mm
    b = width_mm + 2 * gap_mm
    k = a / b
    k_prime = math.sqrt(1 - k * k)

    # Effective permittivity from the geometry
    k1 = math.tanh(math.pi * a / (4 * height_mm)) / math.tanh(
        math.pi * b / (4 * height_mm)
    )
    k1_prime = math.sqrt(1 - k1 * k1)
    er_eff = (
        1 + er * (_K_K_prime(k1) / _K_K_prime(k))
    ) / (1 + (_K_K_prime(k1) / _K_K_prime(k)))

    return (60 / math.sqrt(er_eff)) * (1 / _K_K_prime(k))


def _K_K_prime(k: float) -> float:
    """Approximate K(k) / K(k') using Hilberg's expansion (good to ~ppm)."""
    if k < 1e-12:
        return 1e9
    if k > 1 - 1e-12:
        return 0.0
    k_prime = math.sqrt(1 - k * k)
    if k <= 0.7071:  # ~ 1/sqrt(2)
        # K(k')/K(k) ≈ (1/π) × ln(2 × (1 + sqrt(k')) / (1 − sqrt(k')))
        return math.pi / math.log(
            2 * (1 + math.sqrt(k_prime)) / (1 - math.sqrt(k_prime))
        )
    # Symmetric branch
    return math.log(2 * (1 + math.sqrt(k)) / (1 - math.sqrt(k))) / math.pi


def solve_microstrip_width(
    target_impedance_ohms: float,
    height_mm: float,
    er: float = 4.5,
    thickness_mm: float = 0.035,
    tolerance_ohms: float = 0.1,
    max_iter: int = 50,
) -> float:
    """Bisect for the trace width that achieves `target_impedance_ohms`.

    Range: 0.05 mm ≤ width ≤ 10 mm. Raises if target is outside achievable band.
    """
    f = lambda w: microstrip_impedance(w, height_mm, er, thickness_mm) - target_impedance_ohms

    lo, hi = 0.05, 10.0
    f_lo, f_hi = f(lo), f(hi)
    if f_lo * f_hi > 0:
        z_lo = microstrip_impedance(lo, height_mm, er, thickness_mm)
        z_hi = microstrip_impedance(hi, height_mm, er, thickness_mm)
        raise ValueError(
            f"target {target_impedance_ohms} Ω is outside achievable band "
            f"[{z_hi:.1f}, {z_lo:.1f}] Ω for h={height_mm}mm, er={er}"
        )

    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        f_mid = f(mid)
        if abs(f_mid) <= tolerance_ohms:
            return mid
        if f_lo * f_mid < 0:
            hi = mid
            f_hi = f_mid
        else:
            lo = mid
            f_lo = f_mid
    return 0.5 * (lo + hi)


# --------------------------------------------------------------------------- #
# Trace current capacity (IPC-2152)
# --------------------------------------------------------------------------- #


def trace_current_ipc2152(
    width_mm: float,
    *,
    copper_thickness_mm: float = 0.035,  # 1 oz default
    temp_rise_c: float = 10.0,           # mild temp rise
    location: Literal["external", "internal"] = "external",
) -> float:
    """Estimate sustained current capacity using IPC-2152.

    I = k × ΔT^0.44 × A^0.725

    where A = cross-sectional area (mils²), and k = 0.048 (external surface
    layer) or 0.024 (internal layer with worse heat dissipation).

    Args:
        width_mm:           trace width
        copper_thickness_mm: 0.035 = 1 oz, 0.07 = 2 oz, 0.0175 = 0.5 oz
        temp_rise_c:        allowed delta-T above ambient (10°C is conservative)
        location:           "external" (top/bottom) or "internal" (inner layer)

    Returns:
        Current in amps (steady-state).
    """
    if width_mm <= 0 or copper_thickness_mm <= 0:
        raise ValueError("width and thickness must be positive")
    if temp_rise_c <= 0:
        raise ValueError("temp rise must be > 0")

    # Convert to mils (IPC-2152's native unit)
    width_mils = width_mm / 0.0254
    thickness_mils = copper_thickness_mm / 0.0254
    area_mils2 = width_mils * thickness_mils

    k = 0.048 if location == "external" else 0.024
    return k * (temp_rise_c ** 0.44) * (area_mils2 ** 0.725)


def solve_trace_width_for_current(
    current_a: float,
    *,
    copper_thickness_mm: float = 0.035,
    temp_rise_c: float = 10.0,
    location: Literal["external", "internal"] = "external",
) -> float:
    """Inverse IPC-2152 — solve for the minimum width to carry `current_a`.

    Returns the trace width in mm.
    """
    if current_a <= 0:
        raise ValueError("current must be positive")
    k = 0.048 if location == "external" else 0.024
    # I = k × ΔT^0.44 × A^0.725
    # A = (I / (k × ΔT^0.44))^(1/0.725)
    area_mils2 = (current_a / (k * temp_rise_c ** 0.44)) ** (1 / 0.725)
    thickness_mils = copper_thickness_mm / 0.0254
    width_mils = area_mils2 / thickness_mils
    return width_mils * 0.0254


# --------------------------------------------------------------------------- #
# Helpful presets
# --------------------------------------------------------------------------- #


COMMON_TARGETS = {
    "USB_2.0_diff": {"impedance_ohms": 90, "type": "differential", "notes": "USB 2.0 D+/D−"},
    "Ethernet_diff": {"impedance_ohms": 100, "type": "differential", "notes": "100BASE-T pairs"},
    "HDMI_diff": {"impedance_ohms": 100, "type": "differential", "notes": "HDMI TMDS"},
    "MIPI_DSI_diff": {"impedance_ohms": 100, "type": "differential", "notes": "MIPI D-PHY"},
    "PCIe_diff": {"impedance_ohms": 85, "type": "differential", "notes": "PCIe Gen3+"},
    "RF_50ohm": {"impedance_ohms": 50, "type": "single", "notes": "Standard RF (Wi-Fi, GSM, BT)"},
    "RF_75ohm": {"impedance_ohms": 75, "type": "single", "notes": "Video / cable TV"},
}
