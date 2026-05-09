"""Phase 14 — RF PCB design utilities.

Tools (3):
    add_via_array        — row of vias along a line (for stitching, fencing)
    add_ground_stitching — pair of via fences flanking a trace (RF isolation)
    add_rf_microstrip    — track at the calculated width for a target Z₀
"""

from __future__ import annotations

import logging
from pathlib import Path

from kicad_claude import state
from kicad_claude.adapters import electrical_calc as ec
from kicad_claude.adapters import pcb_editor as ed
from kicad_claude.adapters import sch_editor, sch_io

logger = logging.getLogger("kicad-claude.tools.rf")


def _save_with_backup(tree: list, pcb_path: Path) -> Path | None:
    backup = sch_editor.backup_file(pcb_path)
    sch_io.write_file(pcb_path, tree)
    return backup


def register(mcp) -> None:
    @mcp.tool()
    def add_via_array(
        start_x_mm: float,
        start_y_mm: float,
        end_x_mm: float,
        end_y_mm: float,
        spacing_mm: float = 2.5,
        drill_mm: float = 0.3,
        diameter_mm: float = 0.6,
        net_name: str = "GND",
        perpendicular_offset_mm: float = 0.0,
    ) -> dict:
        """Place a row of vias along a line. Common uses:

        - **Ground stitching** along a trace (offset = ±0.5 mm perpendicular)
        - **Plane stitching** (offset = 0 across a region)
        - **EMI fencing** along sensitive nets

        At RF, place vias at λ/20 spacing or tighter. At 2.4 GHz on FR4
        (εᵣ ≈ 4.5) λ ≈ 60 mm so ~3 mm spacing or finer is appropriate.
        """
        pcb_path = state.get_active_board_path()
        tree = sch_io.parse_file(pcb_path)
        # Allow nets that don't exist yet — pass None to skip net assignment.
        net_arg = net_name if net_name and ed.find_net_index(tree, net_name) is not None else None
        nodes = ed.add_via_array_along_line(
            tree,
            start_mm=(start_x_mm, start_y_mm),
            end_mm=(end_x_mm, end_y_mm),
            spacing_mm=spacing_mm,
            drill_mm=drill_mm,
            diameter_mm=diameter_mm,
            perpendicular_offset_mm=perpendicular_offset_mm,
            net_name=net_arg,
        )
        backup = _save_with_backup(tree, pcb_path)
        return {
            "via_count": len(nodes),
            "spacing_mm": spacing_mm,
            "from_mm": [start_x_mm, start_y_mm],
            "to_mm": [end_x_mm, end_y_mm],
            "perpendicular_offset_mm": perpendicular_offset_mm,
            "net": net_arg or "(unconnected)",
            "backup": str(backup) if backup else None,
        }

    @mcp.tool()
    def add_ground_stitching(
        start_x_mm: float,
        start_y_mm: float,
        end_x_mm: float,
        end_y_mm: float,
        offset_mm: float = 0.5,
        spacing_mm: float = 2.5,
        drill_mm: float = 0.3,
        diameter_mm: float = 0.6,
        net_name: str = "GND",
    ) -> dict:
        """Add two parallel via fences alongside a trace, both connected to GND.

        For RF / high-speed signals: containment of the field, prevention
        of crosstalk to neighbouring traces. The two via rows are placed
        ±`offset_mm` perpendicular to the line (start, end).

        Typical numbers: offset 0.5–1.0 mm from the signal edge, spacing
        2.0–3.0 mm at 2.4 GHz, drill 0.3 mm, diameter 0.6 mm.
        """
        pcb_path = state.get_active_board_path()
        tree = sch_io.parse_file(pcb_path)
        net_arg = net_name if ed.find_net_index(tree, net_name) is not None else None

        upper = ed.add_via_array_along_line(
            tree,
            start_mm=(start_x_mm, start_y_mm),
            end_mm=(end_x_mm, end_y_mm),
            spacing_mm=spacing_mm,
            drill_mm=drill_mm, diameter_mm=diameter_mm,
            perpendicular_offset_mm=+offset_mm,
            net_name=net_arg,
        )
        lower = ed.add_via_array_along_line(
            tree,
            start_mm=(start_x_mm, start_y_mm),
            end_mm=(end_x_mm, end_y_mm),
            spacing_mm=spacing_mm,
            drill_mm=drill_mm, diameter_mm=diameter_mm,
            perpendicular_offset_mm=-offset_mm,
            net_name=net_arg,
        )
        backup = _save_with_backup(tree, pcb_path)
        return {
            "vias_added": len(upper) + len(lower),
            "rows": 2,
            "offset_mm": offset_mm,
            "spacing_mm": spacing_mm,
            "net": net_arg or "(unconnected)",
            "backup": str(backup) if backup else None,
        }

    @mcp.tool()
    def add_rf_microstrip(
        start_x_mm: float,
        start_y_mm: float,
        end_x_mm: float,
        end_y_mm: float,
        target_impedance_ohms: float = 50.0,
        dielectric_height_mm: float = 0.21,  # typical 4-layer prepreg
        dielectric_constant: float = 4.5,
        copper_thickness_mm: float = 0.035,
        layer: str = "F.Cu",
        net_name: str | None = None,
    ) -> dict:
        """Add a track at the width that achieves `target_impedance_ohms`.

        Defaults: 50 Ω microstrip on FR4 with 0.21 mm dielectric (typical
        prepreg in a 4-layer 1.6 mm stackup). Adjust `dielectric_height_mm`
        and `dielectric_constant` to match your stackup.

        Returns the calculated width and the resulting impedance.
        """
        width = ec.solve_microstrip_width(
            target_impedance_ohms,
            dielectric_height_mm,
            er=dielectric_constant,
            thickness_mm=copper_thickness_mm,
        )
        z_check = ec.microstrip_impedance(
            width, dielectric_height_mm,
            er=dielectric_constant,
            thickness_mm=copper_thickness_mm,
        )

        pcb_path = state.get_active_board_path()
        tree = sch_io.parse_file(pcb_path)
        net_idx = 0
        if net_name:
            idx = ed.find_net_index(tree, net_name)
            if idx is None:
                raise KeyError(f"net {net_name!r} not found")
            net_idx = idx
        ed.add_track(
            tree,
            start_x_mm, start_y_mm, end_x_mm, end_y_mm,
            width_mm=width, layer=layer, net=net_idx,
        )
        backup = _save_with_backup(tree, pcb_path)
        return {
            "track_width_mm": round(width, 4),
            "target_impedance_ohms": target_impedance_ohms,
            "achieved_impedance_ohms": round(z_check, 2),
            "skew_ohms": round(z_check - target_impedance_ohms, 3),
            "from_mm": [start_x_mm, start_y_mm],
            "to_mm": [end_x_mm, end_y_mm],
            "layer": layer,
            "stackup": {
                "dielectric_height_mm": dielectric_height_mm,
                "dielectric_constant": dielectric_constant,
                "copper_thickness_mm": copper_thickness_mm,
            },
            "net": net_name or "(unconnected)",
            "backup": str(backup) if backup else None,
        }
