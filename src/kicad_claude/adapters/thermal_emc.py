"""Phase 15 — closed-form thermal and EMC analysis helpers.

These are NOT real FEM simulations. For thermal we use a lumped-element
resistive model (junction → case → ambient, optional board-spreader
coupling). For crosstalk we use a coupled-microstrip approximation. Real
FEM (Ansys Icepak, Sonnet, OpenEMS) is what production hardware uses; our
math gives ballpark numbers good for early-stage design and sanity checks.

Sources:
    - JEDEC JESD51 thermal resistance definitions
    - Microwaves101 crosstalk coupling formulas
    - Henry Ott, Electromagnetic Compatibility Engineering
"""

from __future__ import annotations

import math
from typing import Iterable


# --------------------------------------------------------------------------- #
# Steady-state thermal network
# --------------------------------------------------------------------------- #


def solve_thermal_network(
    components: list[dict],
    ambient_c: float = 25.0,
) -> list[dict]:
    """Solve junction temperatures for a list of components.

    Each component is a dict:
        - reference     : str
        - power_w       : float, dissipation in watts (steady-state)
        - r_jc_c_per_w  : float, junction-to-case thermal resistance (°C/W)
                          (defaults to 1.0 if missing)
        - r_ca_c_per_w  : float, case-to-ambient thermal resistance (°C/W)
                          (defaults to 50.0 if missing — typical SMD without heatsink)

    Total resistance = R_jc + R_ca; junction temp = ambient + power × R_total.

    No coupling between components (each treated independently). For
    PCB-spreader analysis, lump the spreader into R_ca and reduce its
    value (e.g. 20 °C/W with copper pour).
    """
    out = []
    for comp in components:
        ref = comp.get("reference", "?")
        power = float(comp.get("power_w", 0))
        r_jc = float(comp.get("r_jc_c_per_w", 1.0))
        r_ca = float(comp.get("r_ca_c_per_w", 50.0))
        r_total = r_jc + r_ca
        delta_t = power * r_total
        t_junction = ambient_c + delta_t
        out.append({
            "reference": ref,
            "power_w": power,
            "r_jc_c_per_w": r_jc,
            "r_ca_c_per_w": r_ca,
            "r_total_c_per_w": round(r_total, 2),
            "delta_t_c": round(delta_t, 2),
            "junction_temp_c": round(t_junction, 2),
            "warning": "" if t_junction < 85 else (
                "approaching commercial-grade limit (85°C)"
                if t_junction < 105 else
                "exceeds commercial limit; needs heatsink or layout change"
            ),
        })
    out.sort(key=lambda c: -c["junction_temp_c"])  # hottest first
    return out


# --------------------------------------------------------------------------- #
# Crosstalk between coupled microstrips
# --------------------------------------------------------------------------- #


def estimate_crosstalk_coupling(
    parallel_length_mm: float,
    separation_mm: float,
    dielectric_height_mm: float,
    rise_time_ns: float = 1.0,
    er: float = 4.5,
) -> dict:
    """Estimate near-end (NEXT) and far-end (FEXT) crosstalk for parallel microstrips.

    Closed-form approximation (good to ~20% vs FEM):
        K_coupling ≈ 1 / (1 + (s/h)²)
        NEXT_ratio  ≈ K / 4 × (1 − exp(-T_d / τ_r))
        FEXT_ratio  ≈ K × T_d / τ_r          (saturates at K)

    where s = trace separation, h = dielectric height, T_d = propagation
    delay over the parallel section, τ_r = signal rise time.

    Returns ratios (0–1); caller multiplies by aggressor amplitude to get
    induced voltage on the victim trace.
    """
    if separation_mm <= 0 or dielectric_height_mm <= 0:
        raise ValueError("separation and dielectric height must be positive")
    if parallel_length_mm <= 0 or rise_time_ns <= 0:
        raise ValueError("length and rise time must be positive")

    s_over_h = separation_mm / dielectric_height_mm
    k = 1.0 / (1.0 + s_over_h ** 2)

    # Propagation delay on FR4 microstrip: ~ 6 ps/mm (effective εᵣ ≈ 3)
    er_eff = (er + 1) / 2 + (er - 1) / 2 * (1 / math.sqrt(1 + 12 * dielectric_height_mm / 0.4))
    speed_mm_per_ns = 300 / math.sqrt(er_eff)  # speed of light / sqrt(εeff), in mm/ns
    t_d_ns = parallel_length_mm / speed_mm_per_ns

    # NEXT saturates with length / rise time
    next_ratio = (k / 4.0) * (1 - math.exp(-t_d_ns / rise_time_ns))
    # FEXT grows with length / rise time, capped at K
    fext_raw = k * t_d_ns / rise_time_ns
    fext_ratio = min(k, fext_raw)

    return {
        "geometry": {
            "parallel_length_mm": parallel_length_mm,
            "separation_mm": separation_mm,
            "dielectric_height_mm": dielectric_height_mm,
            "rise_time_ns": rise_time_ns,
            "er_eff": round(er_eff, 3),
        },
        "coupling_factor": round(k, 4),
        "propagation_delay_ns": round(t_d_ns, 4),
        "near_end_crosstalk_ratio": round(next_ratio, 6),
        "far_end_crosstalk_ratio": round(fext_ratio, 6),
        "near_end_dB": round(20 * math.log10(max(next_ratio, 1e-9)), 1),
        "far_end_dB": round(20 * math.log10(max(fext_ratio, 1e-9)), 1),
        "warning": "" if next_ratio < 0.05 else (
            "near-end crosstalk >5% — increase separation or shorten parallel run"
        ),
    }


# --------------------------------------------------------------------------- #
# Return path heuristic (EMC)
# --------------------------------------------------------------------------- #


def check_return_path(
    signal_segments: Iterable[dict],
    ground_zone_areas: list[dict],
) -> list[dict]:
    """Heuristic: warn when a high-speed signal traces over a region without GND coverage.

    Each segment dict has: net, layer, start (x, y) KiCAD coords, end (x, y).
    Each ground_zone_areas entry has: layer, polygon (list of (x, y)).

    For each signal segment we sample 10 points along it and check whether
    the GND zone on the OPPOSITE layer (F.Cu signal → B.Cu ground; or any
    inner-layer reference) covers each sample. Segments with <80% coverage
    are flagged.

    This is a coarse check — it doesn't know about reference plane splits
    inside zones, vias, or signal-to-aggressor coupling. Use to find
    obvious problems (signal over a void).
    """
    def _point_in_polygon(x: float, y: float, poly: list[tuple[float, float]]) -> bool:
        """Standard ray-cast inside-polygon test."""
        n = len(poly)
        if n < 3:
            return False
        inside = False
        j = n - 1
        for i in range(n):
            xi, yi = poly[i]
            xj, yj = poly[j]
            if ((yi > y) != (yj > y)) and (
                x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi
            ):
                inside = not inside
            j = i
        return inside

    findings: list[dict] = []
    for seg in signal_segments:
        # Pick the GND zone that's a reference for this signal.
        # Simplified rule: if signal on F.Cu → look for GND on B.Cu (or any
        # inner Cu); else look for any GND zone on a different layer.
        sig_layer = seg.get("layer", "")
        candidate_zones = [
            z for z in ground_zone_areas
            if z.get("layer") and z["layer"] != sig_layer
        ]
        if not candidate_zones:
            findings.append({
                "net": seg.get("net", "?"),
                "layer": sig_layer,
                "coverage_pct": 0,
                "issue": "no GND reference plane on any other layer",
            })
            continue

        sx, sy = seg["start"]
        ex, ey = seg["end"]
        n_samples = 10
        covered = 0
        for i in range(n_samples + 1):
            t = i / n_samples
            x = sx + t * (ex - sx)
            y = sy + t * (ey - sy)
            for z in candidate_zones:
                poly = z.get("polygon") or []
                if _point_in_polygon(x, y, poly):
                    covered += 1
                    break
        pct = round(100 * covered / (n_samples + 1), 1)
        if pct < 80:
            findings.append({
                "net": seg.get("net", "?"),
                "layer": sig_layer,
                "coverage_pct": pct,
                "issue": f"only {pct}% of trace has GND reference below",
            })
    return findings
