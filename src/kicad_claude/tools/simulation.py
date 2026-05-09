"""Phase 15 — pseudo-FEM thermal and EMC analysis tools.

Tools (3):
    simulate_thermal_steady_state    — junction temps via lumped resistive model
    estimate_crosstalk               — closed-form NEXT/FEXT for parallel microstrips
    check_return_path_continuity     — flag signal traces with poor GND reference

These are NOT real FEM solvers. For production hardware that ships at
volume, validate with Ansys Icepak / Sonnet / OpenEMS. Our outputs are
ballpark estimates good for early-stage layout decisions.
"""

from __future__ import annotations

import logging

from kicad_claude import state
from kicad_claude.adapters import pcb_editor as pcb_ed
from kicad_claude.adapters import sch_io, thermal_emc

logger = logging.getLogger("kicad-claude.tools.simulation")


def register(mcp) -> None:
    @mcp.tool()
    def simulate_thermal_steady_state(
        components: list,
        ambient_c: float = 25.0,
    ) -> dict:
        """Compute steady-state junction temperatures for components dissipating power.

        Each entry in `components` is a dict with:
            reference     : str (e.g. "U1", "Q1")
            power_w       : float, dissipation in W
            r_jc_c_per_w  : float (default 1.0) — junction-to-case Rth
            r_ca_c_per_w  : float (default 50)  — case-to-ambient Rth (no heatsink)

        Common Rth values:
            SMD SOIC-8 free air:    ~120 °C/W
            SMD SOIC-8 on copper:   ~50-80 °C/W
            TO-220 free air:        ~62 °C/W
            TO-220 on heatsink:     ~5-10 °C/W
            QFN/DFN:                ~30-50 °C/W

        Returns sorted list (hottest first) with junction temp + warning when
        approaching/exceeding 85 °C (commercial grade limit).
        """
        results = thermal_emc.solve_thermal_network(list(components), ambient_c=ambient_c)
        # Aggregate stats
        max_t = max((r["junction_temp_c"] for r in results), default=ambient_c)
        total_power = sum(float(c.get("power_w", 0)) for c in components)
        return {
            "ambient_c": ambient_c,
            "component_count": len(results),
            "total_dissipation_w": round(total_power, 3),
            "max_junction_temp_c": round(max_t, 2),
            "components": results,
        }

    @mcp.tool()
    def estimate_crosstalk(
        parallel_length_mm: float,
        separation_mm: float,
        dielectric_height_mm: float = 0.21,
        rise_time_ns: float = 1.0,
        dielectric_constant: float = 4.5,
    ) -> dict:
        """Closed-form NEXT/FEXT estimate for two parallel microstrips.

        Inputs:
            parallel_length_mm:  length over which the two traces run side by side
            separation_mm:       center-to-center (or edge-to-edge) gap
            dielectric_height_mm: height of the trace above its reference plane
            rise_time_ns:        signal rise time (faster = more crosstalk)
            dielectric_constant: εᵣ (FR4 ≈ 4.5; Rogers RO4350B = 3.66)

        Output ratios (0–1) are the fraction of the aggressor amplitude that
        couples onto the victim line. Multiply by the aggressor V_pp to get
        induced voltage. Rule of thumb: keep NEXT < 5% (−26 dB) for general
        digital, < 1% (−40 dB) for sensitive analog/RF.
        """
        return thermal_emc.estimate_crosstalk_coupling(
            parallel_length_mm=parallel_length_mm,
            separation_mm=separation_mm,
            dielectric_height_mm=dielectric_height_mm,
            rise_time_ns=rise_time_ns,
            er=dielectric_constant,
        )

    @mcp.tool()
    def check_return_path_continuity(
        signal_nets: str = "",
        max_segments_to_check: int = 200,
    ) -> dict:
        """Find signal traces that don't have a GND reference plane underneath.

        For each track on the active PCB, we sample 10 points along it and
        check whether they're inside ANY GND zone polygon on a different
        layer. Segments with <80% coverage are flagged — the signal's
        return current has nowhere clean to flow there, which causes EMI
        and impedance discontinuities.

        `signal_nets`: comma-separated list of net names to check (default:
        all nets except GND/+5V/+3V3/etc). Limited to `max_segments_to_check`
        per net for speed.
        """
        tree = sch_io.parse_file(state.get_active_board_path())

        # Collect GND zones (polygon in KiCAD coords)
        gnd_zones: list[dict] = []
        for z in sch_io.find_children(tree, "zone"):
            net_name_node = sch_io.find_child(z, "net_name")
            if not (net_name_node and len(net_name_node) >= 2):
                continue
            net_name = net_name_node[1]
            if net_name not in ("GND", "0V", "VSS", "AGND", "DGND"):
                continue
            layer_node = sch_io.find_child(z, "layer")
            layer = layer_node[1] if layer_node and len(layer_node) >= 2 else ""
            polygon = sch_io.find_child(z, "polygon")
            if not polygon:
                continue
            pts = sch_io.find_child(polygon, "pts")
            if not pts:
                continue
            poly_points = []
            for c in pts[1:]:
                if sch_io.is_call(c, "xy") and len(c) >= 3:
                    poly_points.append((float(c[1]), float(c[2])))
            if poly_points:
                gnd_zones.append({"layer": layer, "polygon": poly_points})

        # Collect signal segments
        net_filter = set()
        if signal_nets.strip():
            net_filter = {n.strip() for n in signal_nets.split(",") if n.strip()}

        idx_to_name = {n["index"]: n["name"] for n in pcb_ed.list_nets(tree)}
        skip_nets = {"GND", "+5V", "+3V3", "+3.3V", "+12V", "+VBUS", "VCC", "VDD",
                     "AGND", "DGND", "VSS", "0V"}

        segments: list[dict] = []
        for seg in sch_io.find_children(tree, "segment"):
            net_node = sch_io.find_child(seg, "net")
            start = sch_io.find_child(seg, "start")
            end = sch_io.find_child(seg, "end")
            layer = sch_io.find_child(seg, "layer")
            if not (net_node and start and end and layer):
                continue
            net_idx = int(net_node[1])
            net_name = idx_to_name.get(net_idx, f"<net{net_idx}>")
            if net_filter and net_name not in net_filter:
                continue
            if not net_filter and net_name in skip_nets:
                continue
            segments.append({
                "net": net_name,
                "layer": layer[1],
                "start": (float(start[1]), float(start[2])),
                "end": (float(end[1]), float(end[2])),
            })
            if len(segments) >= max_segments_to_check:
                break

        findings = thermal_emc.check_return_path(segments, gnd_zones)
        return {
            "segments_checked": len(segments),
            "gnd_zones": [{"layer": z["layer"], "vertex_count": len(z["polygon"])} for z in gnd_zones],
            "findings": findings,
            "issue_count": len(findings),
            "tip": "Run add_ground_plane on a back layer to fix most return-path issues.",
        }
