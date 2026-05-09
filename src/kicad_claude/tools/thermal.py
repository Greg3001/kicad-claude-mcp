"""Phase 14 — thermal / current-capacity tools (IPC-2152).

Tools (3):
    calculate_trace_current_capacity   — Amps a trace can carry at given ΔT
    solve_trace_width_for_current      — minimum width to carry given current
    analyze_pcb_current_capacity       — per-track scan of the active PCB
"""

from __future__ import annotations

import logging

from kicad_claude import state
from kicad_claude.adapters import electrical_calc as ec
from kicad_claude.adapters import pcb_editor as pcb_ed
from kicad_claude.adapters import sch_io

logger = logging.getLogger("kicad-claude.tools.thermal")


def register(mcp) -> None:
    @mcp.tool()
    def calculate_trace_current_capacity(
        width_mm: float,
        copper_oz: float = 1.0,
        temp_rise_c: float = 10.0,
        location: str = "external",
    ) -> dict:
        """How much current a trace can carry per IPC-2152.

        Args:
            width_mm:        trace width
            copper_oz:       copper weight (1.0 default; 0.5 / 1 / 2 are common)
            temp_rise_c:     allowed temperature rise above ambient (10°C is conservative)
            location:        "external" (top/bottom — better cooling) or "internal" (inner layers)

        Returns the steady-state current in amps. For pulse loads, derate
        appropriately or consult IPC-2221 supplemental data.
        """
        if location not in ("external", "internal"):
            raise ValueError(f"location must be 'external' or 'internal' (got {location!r})")
        thickness_mm = copper_oz * 0.035
        amps = ec.trace_current_ipc2152(
            width_mm,
            copper_thickness_mm=thickness_mm,
            temp_rise_c=temp_rise_c,
            location=location,
        )
        return {
            "current_a": round(amps, 3),
            "width_mm": width_mm,
            "copper_oz": copper_oz,
            "copper_thickness_mm": thickness_mm,
            "temp_rise_c": temp_rise_c,
            "location": location,
        }

    @mcp.tool()
    def solve_trace_width_for_current(
        current_a: float,
        copper_oz: float = 1.0,
        temp_rise_c: float = 10.0,
        location: str = "external",
        margin_pct: float = 20.0,
    ) -> dict:
        """Minimum trace width to carry `current_a` per IPC-2152, with margin.

        `margin_pct` adds a safety factor (default 20%) so the recommended
        width carries the target current at the chosen ΔT plus headroom.
        """
        if location not in ("external", "internal"):
            raise ValueError(f"location must be 'external' or 'internal' (got {location!r})")
        thickness_mm = copper_oz * 0.035
        target_current = current_a * (1 + margin_pct / 100.0)
        w = ec.solve_trace_width_for_current(
            target_current,
            copper_thickness_mm=thickness_mm,
            temp_rise_c=temp_rise_c,
            location=location,
        )
        # Verify
        achieved = ec.trace_current_ipc2152(
            w, copper_thickness_mm=thickness_mm,
            temp_rise_c=temp_rise_c, location=location,
        )
        return {
            "recommended_width_mm": round(w, 4),
            "achieved_current_a": round(achieved, 3),
            "target_current_a": current_a,
            "margin_pct": margin_pct,
            "copper_oz": copper_oz,
            "temp_rise_c": temp_rise_c,
            "location": location,
        }

    @mcp.tool()
    def analyze_pcb_current_capacity(
        copper_oz: float = 1.0,
        temp_rise_c: float = 10.0,
    ) -> dict:
        """Scan every track in the active PCB and report its current capacity.

        Computes per-segment capacity from its width + layer (external for
        F.Cu/B.Cu, internal otherwise). Groups by net so you see the
        weakest link — the narrowest segment dominates.
        """
        tree, _ = (sch_io.parse_file(state.get_active_board_path()),
                   state.get_active_board_path())
        thickness_mm = copper_oz * 0.035
        per_net: dict[str, dict] = {}

        for seg in sch_io.find_children(tree, "segment"):
            width_node = sch_io.find_child(seg, "width")
            layer_node = sch_io.find_child(seg, "layer")
            net_node = sch_io.find_child(seg, "net")
            if not (width_node and layer_node and net_node):
                continue
            try:
                width_mm = float(width_node[1])
            except (ValueError, TypeError):
                continue
            layer = layer_node[1] if len(layer_node) >= 2 else ""
            location = "external" if layer in ("F.Cu", "B.Cu") else "internal"
            net_idx = int(net_node[1])

            try:
                amps = ec.trace_current_ipc2152(
                    width_mm,
                    copper_thickness_mm=thickness_mm,
                    temp_rise_c=temp_rise_c,
                    location=location,
                )
            except ValueError:
                continue

            # Resolve net name once per net
            net_name = ""
            for n in pcb_ed.list_nets(tree):
                if n["index"] == net_idx:
                    net_name = n["name"]
                    break

            entry = per_net.setdefault(
                net_name or f"<net{net_idx}>",
                {"min_width_mm": float("inf"), "min_capacity_a": float("inf"),
                 "segments": 0, "layers": set()},
            )
            entry["segments"] += 1
            entry["min_width_mm"] = min(entry["min_width_mm"], width_mm)
            entry["min_capacity_a"] = min(entry["min_capacity_a"], amps)
            entry["layers"].add(layer)

        # Format
        result = []
        for net, info in sorted(per_net.items()):
            result.append({
                "net": net,
                "min_width_mm": round(info["min_width_mm"], 4),
                "min_capacity_a": round(info["min_capacity_a"], 3),
                "segment_count": info["segments"],
                "layers": sorted(info["layers"]),
            })
        result.sort(key=lambda r: r["min_capacity_a"])  # weakest first
        return {
            "copper_oz": copper_oz,
            "temp_rise_c": temp_rise_c,
            "total_nets": len(result),
            "weakest_net": result[0] if result else None,
            "nets": result,
        }
