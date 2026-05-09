"""Phase 14 — EMC / EMI heuristic checks.

These are NOT real simulations (which need FEA tools like Ansys / Sonnet).
They are pragmatic checks based on common best-practice rules:

    analyze_ground_coverage  — % of each copper layer occupied by GND zones
    find_long_traces         — antenna candidates (longer = better radiator)
    validate_decoupling_caps — heuristic: any IC pad without a cap within 3 mm
"""

from __future__ import annotations

import logging
import math

from kicad_claude import state
from kicad_claude.adapters import pcb_editor as ed
from kicad_claude.adapters import sch_io

logger = logging.getLogger("kicad-claude.tools.emc")


def _board_area_mm2(tree: list) -> float:
    """Area in mm² of the rectangular board outline (gr_rect on Edge.Cuts)."""
    poly = ed.get_board_outline_polygon_kicad(tree)
    if not poly:
        return 0.0
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    return (max(xs) - min(xs)) * (max(ys) - min(ys))


def _zone_area_mm2(zone_node: list) -> float:
    """Area in mm² of a (zone ...) — assumes its polygon is convex/simple."""
    polygon = sch_io.find_child(zone_node, "polygon")
    if not polygon:
        return 0.0
    pts = sch_io.find_child(polygon, "pts")
    if not pts:
        return 0.0
    coords = []
    for c in pts[1:]:
        if sch_io.is_call(c, "xy") and len(c) >= 3:
            coords.append((float(c[1]), float(c[2])))
    if len(coords) < 3:
        return 0.0
    # Shoelace formula
    n = len(coords)
    s = 0.0
    for i in range(n):
        x1, y1 = coords[i]
        x2, y2 = coords[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2


def register(mcp) -> None:
    @mcp.tool()
    def analyze_ground_coverage() -> dict:
        """Estimate % of each copper layer covered by GND copper zones.

        Higher is generally better for EMC: a continuous reference plane
        improves signal integrity, reduces radiated emissions, and gives
        return currents a clean path. Aim for >70% on at least one layer
        for any board with high-speed signals.

        Note: this counts the *zone* polygon area, not the actual filled
        copper after subtracting clearance to pads/tracks. It's a useful
        upper bound, not the real value (run KiCAD's GUI to see exact).
        """
        tree = sch_io.parse_file(state.get_active_board_path())
        board_area = _board_area_mm2(tree)
        if board_area <= 0:
            return {
                "warning": "no rectangular board outline; can't compute coverage",
                "board_area_mm2": 0,
                "by_layer": {},
            }

        by_layer: dict[str, float] = {}
        zone_count_per_layer: dict[str, int] = {}
        for z in sch_io.find_children(tree, "zone"):
            net_name = sch_io.find_child(z, "net_name")
            if not (net_name and len(net_name) >= 2 and net_name[1] in ("GND", "0V", "VSS")):
                continue
            layer_node = sch_io.find_child(z, "layer")
            if not layer_node or len(layer_node) < 2:
                continue
            layer = layer_node[1]
            area = _zone_area_mm2(z)
            by_layer[layer] = by_layer.get(layer, 0.0) + area
            zone_count_per_layer[layer] = zone_count_per_layer.get(layer, 0) + 1

        report = []
        for layer, area in sorted(by_layer.items()):
            report.append({
                "layer": layer,
                "gnd_zones": zone_count_per_layer.get(layer, 0),
                "zone_area_mm2": round(area, 1),
                "coverage_pct_estimated": round(100 * area / board_area, 1),
            })
        # Warnings
        warnings = []
        copper_layers = ed.get_copper_layer_names(tree)
        for cu in copper_layers:
            if cu not in by_layer:
                warnings.append(f"no GND zone on {cu}")

        return {
            "board_area_mm2": round(board_area, 1),
            "copper_layers": copper_layers,
            "by_layer": report,
            "warnings": warnings,
        }

    @mcp.tool()
    def find_long_traces(threshold_mm: float = 50.0) -> dict:
        """List nets whose total trace length exceeds `threshold_mm`.

        Long traces are antennas — both for emission (loop area = noise
        radiator) and reception (picking up unwanted noise). For high-speed
        signals (>50 MHz) a length over λ/10 can already cause issues:
        - 100 MHz → λ/10 ≈ 200 mm
        - 1 GHz → λ/10 ≈ 20 mm
        - 2.4 GHz → λ/10 ≈ 8 mm

        Use this list to prioritize where to add ground stitching, shorter
        routing, or impedance control.
        """
        tree = sch_io.parse_file(state.get_active_board_path())
        per_net: dict[int, float] = {}
        for seg in sch_io.find_children(tree, "segment"):
            net_node = sch_io.find_child(seg, "net")
            start = sch_io.find_child(seg, "start")
            end = sch_io.find_child(seg, "end")
            if not (net_node and start and end):
                continue
            try:
                net_idx = int(net_node[1])
                length = math.hypot(
                    float(end[1]) - float(start[1]),
                    float(end[2]) - float(start[2]),
                )
            except (ValueError, TypeError):
                continue
            per_net[net_idx] = per_net.get(net_idx, 0.0) + length

        # Resolve names
        idx_to_name = {n["index"]: n["name"] for n in ed.list_nets(tree)}
        long_nets = [
            {
                "net": idx_to_name.get(idx, f"<net{idx}>"),
                "length_mm": round(L, 2),
                "lambda_estimate_at_1GHz_mm": round(L / 200, 2),  # λ at 1GHz on FR4 ≈ 100 mm, λ/10 ≈ 10
            }
            for idx, L in per_net.items()
            if L >= threshold_mm
        ]
        long_nets.sort(key=lambda r: -r["length_mm"])
        return {
            "threshold_mm": threshold_mm,
            "long_nets": long_nets,
            "count": len(long_nets),
        }

    @mcp.tool()
    def validate_decoupling_caps(
        max_distance_mm: float = 3.0,
        ic_reference_prefixes: str = "U,IC",
    ) -> dict:
        """Heuristic: warn about ICs missing a decoupling capacitor nearby.

        For each footprint whose reference starts with one of the given
        prefixes (default U/IC), find the closest capacitor (reference
        starting with C). If the distance exceeds `max_distance_mm`, that
        IC is flagged as potentially missing decoupling.

        Caveat: this is purely geometric — it doesn't check that the cap is
        actually wired to VCC/GND of the IC. It's a starting-point review,
        not a guarantee.
        """
        tree = sch_io.parse_file(state.get_active_board_path())
        prefixes = tuple(p.strip().upper() for p in ic_reference_prefixes.split(",") if p.strip())
        ics: list[tuple[str, float, float]] = []
        caps: list[tuple[str, float, float]] = []
        for fp in ed.iter_footprints(tree):
            ref = ed.get_footprint_reference(fp) or ""
            at = sch_io.find_child(fp, "at")
            if not at or len(at) < 3:
                continue
            x, y = float(at[1]), float(at[2])
            ru = ref.upper()
            if any(ru.startswith(p) for p in prefixes):
                ics.append((ref, x, y))
            elif ru.startswith("C"):
                caps.append((ref, x, y))

        bad: list[dict] = []
        good: list[dict] = []
        for ref, x, y in ics:
            if not caps:
                bad.append({"reference": ref, "nearest_cap": None,
                            "nearest_cap_distance_mm": None})
                continue
            best = min(caps, key=lambda c: math.hypot(c[1] - x, c[2] - y))
            d = math.hypot(best[1] - x, best[2] - y)
            entry = {
                "reference": ref,
                "nearest_cap": best[0],
                "nearest_cap_distance_mm": round(d, 2),
            }
            (good if d <= max_distance_mm else bad).append(entry)
        return {
            "max_distance_mm": max_distance_mm,
            "prefixes": list(prefixes),
            "icas_with_decap": good,
            "icas_missing_decap": bad,
            "ic_count": len(ics),
            "cap_count": len(caps),
        }
