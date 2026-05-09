"""Phase 14 — signal integrity / impedance calculation tools.

Tools (5):
    calculate_microstrip_impedance         — Z₀ for surface trace + reference plane
    calculate_stripline_impedance          — Z₀ for inner trace between two planes
    calculate_differential_impedance       — Z_diff for a coupled microstrip pair
    calculate_coplanar_waveguide_impedance — Z₀ for CPWG (RF-friendly)
    solve_trace_width_for_impedance        — inverse: target Z₀ → width
    list_impedance_targets                 — common presets (USB, Ethernet, HDMI, RF50)
"""

from __future__ import annotations

import logging

from kicad_claude.adapters import electrical_calc as ec

logger = logging.getLogger("kicad-claude.tools.signal_integrity")


def register(mcp) -> None:
    @mcp.tool()
    def calculate_microstrip_impedance(
        width_mm: float,
        dielectric_height_mm: float,
        dielectric_constant: float = 4.5,
        copper_thickness_mm: float = 0.035,
    ) -> dict:
        """Single-ended microstrip Z₀ via IPC-2141A.

        Defaults: FR4 (εᵣ=4.5), 1 oz Cu (0.035 mm). Valid in 0.1 ≤ w/h ≤ 2.0.
        """
        z = ec.microstrip_impedance(
            width_mm, dielectric_height_mm,
            er=dielectric_constant,
            thickness_mm=copper_thickness_mm,
        )
        return {
            "impedance_ohms": round(z, 2),
            "type": "microstrip",
            "geometry": {
                "width_mm": width_mm,
                "dielectric_height_mm": dielectric_height_mm,
                "dielectric_constant": dielectric_constant,
                "copper_thickness_mm": copper_thickness_mm,
            },
            "ratio_w_over_h": round(width_mm / dielectric_height_mm, 3),
        }

    @mcp.tool()
    def calculate_stripline_impedance(
        width_mm: float,
        dielectric_total_mm: float,
        dielectric_constant: float = 4.5,
        copper_thickness_mm: float = 0.035,
    ) -> dict:
        """Stripline Z₀ — inner-layer trace centered between two reference planes.

        `dielectric_total_mm` (b) is the full distance between the upper and
        lower planes; the trace sits in the middle.
        """
        z = ec.stripline_impedance(
            width_mm, dielectric_total_mm,
            er=dielectric_constant,
            thickness_mm=copper_thickness_mm,
        )
        return {
            "impedance_ohms": round(z, 2),
            "type": "stripline",
            "geometry": {
                "width_mm": width_mm,
                "dielectric_total_mm": dielectric_total_mm,
                "dielectric_constant": dielectric_constant,
                "copper_thickness_mm": copper_thickness_mm,
            },
        }

    @mcp.tool()
    def calculate_differential_impedance(
        width_mm: float,
        gap_mm: float,
        dielectric_height_mm: float,
        dielectric_constant: float = 4.5,
        copper_thickness_mm: float = 0.035,
    ) -> dict:
        """Differential microstrip Z_diff (Polar approximation).

        For USB 2.0 target ~90 Ω, HDMI/Ethernet ~100 Ω, MIPI ~100 Ω.
        `gap_mm` is the inter-trace gap; `dielectric_height_mm` is to the
        ground plane below.
        """
        z_diff = ec.differential_microstrip_impedance(
            width_mm, gap_mm, dielectric_height_mm,
            er=dielectric_constant,
            thickness_mm=copper_thickness_mm,
        )
        z0 = ec.microstrip_impedance(
            width_mm, dielectric_height_mm,
            er=dielectric_constant,
            thickness_mm=copper_thickness_mm,
        )
        return {
            "differential_impedance_ohms": round(z_diff, 2),
            "single_ended_impedance_ohms": round(z0, 2),
            "type": "differential_microstrip",
            "geometry": {
                "width_mm": width_mm,
                "gap_mm": gap_mm,
                "dielectric_height_mm": dielectric_height_mm,
                "dielectric_constant": dielectric_constant,
                "copper_thickness_mm": copper_thickness_mm,
            },
        }

    @mcp.tool()
    def calculate_coplanar_waveguide_impedance(
        width_mm: float,
        gap_to_ground_mm: float,
        dielectric_height_mm: float,
        dielectric_constant: float = 4.5,
        copper_thickness_mm: float = 0.035,
    ) -> dict:
        """CPWG (coplanar waveguide with ground) Z₀.

        Used for RF traces. `gap_to_ground_mm` is the spacing between the
        signal trace and the ground pour beside it on the same layer; the
        ground plane below is at `dielectric_height_mm`.
        """
        z = ec.coplanar_waveguide_impedance(
            width_mm, gap_to_ground_mm, dielectric_height_mm,
            er=dielectric_constant,
            thickness_mm=copper_thickness_mm,
        )
        return {
            "impedance_ohms": round(z, 2),
            "type": "coplanar_waveguide_with_ground",
            "geometry": {
                "width_mm": width_mm,
                "gap_to_ground_mm": gap_to_ground_mm,
                "dielectric_height_mm": dielectric_height_mm,
                "dielectric_constant": dielectric_constant,
                "copper_thickness_mm": copper_thickness_mm,
            },
        }

    @mcp.tool()
    def solve_trace_width_for_impedance(
        target_impedance_ohms: float,
        dielectric_height_mm: float,
        dielectric_constant: float = 4.5,
        copper_thickness_mm: float = 0.035,
    ) -> dict:
        """Inverse: width that achieves `target_impedance_ohms` for a microstrip.

        Common targets:
          - 50 Ω: standard RF (Wi-Fi, BT, GSM)
          - 75 Ω: video / cable TV
          - 90 Ω: USB 2.0 single-ended (D+ or D−)
          - 100 Ω: Ethernet / HDMI single-ended
        """
        w = ec.solve_microstrip_width(
            target_impedance_ohms,
            dielectric_height_mm,
            er=dielectric_constant,
            thickness_mm=copper_thickness_mm,
        )
        # Verify
        z_check = ec.microstrip_impedance(
            w, dielectric_height_mm, er=dielectric_constant,
            thickness_mm=copper_thickness_mm,
        )
        return {
            "width_mm": round(w, 4),
            "achieved_impedance_ohms": round(z_check, 2),
            "target_impedance_ohms": target_impedance_ohms,
            "skew_ohms": round(z_check - target_impedance_ohms, 3),
        }

    @mcp.tool()
    def list_impedance_targets() -> dict:
        """Common impedance targets for popular interfaces."""
        return {"presets": ec.COMMON_TARGETS}
