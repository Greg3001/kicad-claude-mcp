"""Phase 10 — design rules and net classes.

Tools (6):
    set_design_rules         — set DRC numerical minimums on the active project
    apply_fab_preset         — load a predefined fab profile (jlcpcb/pcbway/...)
    list_fab_presets         — list available fab presets with descriptions
    add_net_class            — create or update a net class
    assign_net_class         — match nets to a class via pattern (e.g. "+5V")
    list_net_classes         — list all classes + their pattern assignments
"""

from __future__ import annotations

import logging

from kicad_claude import state
from kicad_claude.adapters import drc_rules, project_settings as ps

logger = logging.getLogger("kicad-claude.tools.rules")


def register(mcp) -> None:
    """Register Phase 10 design-rules and net-class tools."""

    @mcp.tool()
    def set_design_rules(
        min_clearance_mm: float | None = None,
        min_track_width_mm: float | None = None,
        min_via_diameter_mm: float | None = None,
        min_via_drill_mm: float | None = None,
        min_through_hole_diameter_mm: float | None = None,
        min_hole_clearance_mm: float | None = None,
        min_hole_to_hole_mm: float | None = None,
        min_silk_clearance_mm: float | None = None,
        min_text_height_mm: float | None = None,
        min_text_thickness_mm: float | None = None,
        min_copper_edge_clearance_mm: float | None = None,
        allow_blind_buried_vias: bool | None = None,
        allow_microvias: bool | None = None,
    ) -> dict:
        """Set numerical DRC rules on the active project's `.kicad_pro`.

        All parameters optional — only non-None values are applied; existing
        rules are preserved otherwise. Run `run_drc` afterwards to verify
        the design fits the new constraints.
        """
        proj = state.get_active()
        pro = ps.load_pro(proj.pro_path)
        ps.update_design_rules(
            pro,
            min_clearance=min_clearance_mm,
            min_track_width=min_track_width_mm,
            min_via_diameter=min_via_diameter_mm,
            min_via_drill=min_via_drill_mm,
            min_through_hole_diameter=min_through_hole_diameter_mm,
            min_hole_clearance=min_hole_clearance_mm,
            min_hole_to_hole=min_hole_to_hole_mm,
            min_silk_clearance=min_silk_clearance_mm,
            min_text_height=min_text_height_mm,
            min_text_thickness=min_text_thickness_mm,
            min_copper_edge_clearance=min_copper_edge_clearance_mm,
            allow_blind_buried_vias=allow_blind_buried_vias,
            allow_microvias=allow_microvias,
        )
        ps.save_pro(proj.pro_path, pro)
        return {"rules": ps.get_design_rules(pro)}

    @mcp.tool()
    def list_fab_presets() -> dict:
        """List the bundled fab presets with their descriptions."""
        return {
            "presets": [
                {"name": name, "description": preset["description"]}
                for name, preset in ps.FAB_PRESETS.items()
            ],
        }

    @mcp.tool()
    def apply_fab_preset(preset: str) -> dict:
        """Apply a bundled fab preset (e.g. 'jlcpcb_2l_default') to the active project.

        Run `list_fab_presets` to see the catalogue.
        """
        if preset not in ps.FAB_PRESETS:
            raise KeyError(
                f"unknown preset {preset!r}. Available: {sorted(ps.FAB_PRESETS)}"
            )
        proj = state.get_active()
        pro = ps.load_pro(proj.pro_path)
        ps.update_design_rules(pro, **ps.FAB_PRESETS[preset]["rules"])
        ps.save_pro(proj.pro_path, pro)
        return {
            "preset": preset,
            "description": ps.FAB_PRESETS[preset]["description"],
            "rules_applied": ps.FAB_PRESETS[preset]["rules"],
        }

    @mcp.tool()
    def add_net_class(
        name: str,
        track_width_mm: float | None = None,
        clearance_mm: float | None = None,
        via_diameter_mm: float | None = None,
        via_drill_mm: float | None = None,
        diff_pair_width_mm: float | None = None,
        diff_pair_gap_mm: float | None = None,
        description: str = "",
    ) -> dict:
        """Create or update a net class. Existing classes get their fields merged.

        Common patterns:
          - Power: track_width 0.5 mm, clearance 0.25 mm
          - Signal: track_width 0.2 mm, clearance 0.15 mm
          - USB_DP: diff_pair_width 0.15 mm, diff_pair_gap 0.15 mm
        """
        proj = state.get_active()
        pro = ps.load_pro(proj.pro_path)
        cls = ps.add_or_update_net_class(
            pro, name,
            track_width_mm=track_width_mm,
            clearance_mm=clearance_mm,
            via_diameter_mm=via_diameter_mm,
            via_drill_mm=via_drill_mm,
            diff_pair_width_mm=diff_pair_width_mm,
            diff_pair_gap_mm=diff_pair_gap_mm,
            description=description,
        )
        ps.save_pro(proj.pro_path, pro)
        return {"net_class": cls}

    @mcp.tool()
    def add_diff_pair_class(
        name: str,
        diff_pair_width_mm: float = 0.15,
        diff_pair_gap_mm: float = 0.15,
        track_width_mm: float | None = None,
        clearance_mm: float | None = None,
        via_diameter_mm: float | None = None,
        via_drill_mm: float | None = None,
        description: str = "",
    ) -> dict:
        """Convenience: create a net class tuned for differential pair routing.

        Sets `diff_pair_width` and `diff_pair_gap` (the two fields KiCAD's
        DSN exporter writes into the Specctra file so Freerouting routes
        the pair coupled). `track_width` etc. are optional overrides.

        Common targets:
          - USB 2.0 90Ω: diff_pair_width 0.2 mm, gap 0.18 mm
          - Ethernet 100Ω: diff_pair_width 0.3 mm, gap 0.2 mm
          - HDMI 100Ω: diff_pair_width 0.18 mm, gap 0.18 mm

        After creating the class, use `assign_net_class` with patterns
        matching both pair members (e.g. "USB_*") and ensure your nets are
        named with `_P/_N`, `+/-`, or `DP/DM` so KiCAD pairs them correctly.
        """
        proj = state.get_active()
        pro = ps.load_pro(proj.pro_path)
        cls = ps.add_or_update_net_class(
            pro, name,
            track_width_mm=track_width_mm,
            clearance_mm=clearance_mm,
            via_diameter_mm=via_diameter_mm,
            via_drill_mm=via_drill_mm,
            diff_pair_width_mm=diff_pair_width_mm,
            diff_pair_gap_mm=diff_pair_gap_mm,
            description=description or f"Diff pair class — {diff_pair_width_mm}/{diff_pair_gap_mm} mm",
        )
        ps.save_pro(proj.pro_path, pro)
        return {
            "net_class": cls,
            "tip": (
                "Run `assign_net_class` with patterns like 'USB_*' or 'HDMI_*' "
                "to bind your diff pair nets. Make sure pair members use "
                "_P/_N, +/-, or DP/DM suffixes so Freerouting recognizes them."
            ),
        }

    @mcp.tool()
    def remove_net_class(name: str) -> dict:
        """Remove a net class. Also drops any pattern assignments referring to it."""
        proj = state.get_active()
        pro = ps.load_pro(proj.pro_path)
        removed = ps.remove_net_class(pro, name)
        if not removed:
            raise KeyError(f"no net class named {name!r}")
        ps.save_pro(proj.pro_path, pro)
        return {"removed": name}

    @mcp.tool()
    def assign_net_class(net_pattern: str, class_name: str) -> dict:
        """Match nets to a class by pattern (KiCAD glob, e.g. '+5V', 'GND', 'USB_*').

        The class must exist (use `add_net_class` first). Idempotent on
        (pattern, class) pairs.
        """
        proj = state.get_active()
        pro = ps.load_pro(proj.pro_path)
        entry = ps.assign_pattern(pro, netclass=class_name, pattern=net_pattern)
        ps.save_pro(proj.pro_path, pro)
        return {"assignment": entry}

    # ----- Custom DRC rules (.kicad_dru) -------------------------------- #

    @mcp.tool()
    def add_drc_rule(
        name: str,
        constraint_type: str,
        min_value_mm: float | None = None,
        opt_value_mm: float | None = None,
        max_value_mm: float | None = None,
        condition: str = "",
        severity: str = "error",
        layer: str = "",
    ) -> dict:
        """Add a custom rule to the project's `.kicad_dru` file.

        `constraint_type` examples:
          - clearance, hole_clearance, edge_clearance, courtyard_clearance
          - track_width, via_diameter, via_drill, hole_size
          - diff_pair_gap, diff_pair_uncoupled
          - length, skew (for matched-length groups)
          - silk_clearance, text_height, text_thickness
          - disallow (different syntax — pass condition only)

        `condition` is KiCAD's expression language, e.g.:
          - "A.NetClass == 'Power'"
          - "A.Layer == 'F.Cu' && B.Layer == 'F.Cu'"
          - "A.Net == '+5V' || B.Net == '+5V'"
          - "A.intersectsArea('keepout1')"

        Run `run_drc()` after to enforce. Replaces any existing rule with
        the same name.
        """
        proj = state.get_active()
        rule = {
            "name": name,
            "constraint_type": constraint_type,
            "severity": severity,
        }
        if condition:
            rule["condition"] = condition
        if layer:
            rule["layer"] = layer
        if min_value_mm is not None:
            rule["min_value"] = min_value_mm
        if opt_value_mm is not None:
            rule["opt_value"] = opt_value_mm
        if max_value_mm is not None:
            rule["max_value"] = max_value_mm

        drc_rules.validate_rule(rule)

        dru_path = proj.path / f"{proj.name}.kicad_dru"
        rules = drc_rules.read_rules(dru_path)
        rules = [r for r in rules if r.get("name") != name]
        rules.append(rule)
        drc_rules.write_rules(dru_path, rules)
        return {
            "rule": rule,
            "dru_path": str(dru_path),
            "total_rules": len(rules),
        }

    @mcp.tool()
    def list_drc_rules() -> dict:
        """List the custom DRC rules in the project's `.kicad_dru` file."""
        proj = state.get_active()
        dru_path = proj.path / f"{proj.name}.kicad_dru"
        rules = drc_rules.read_rules(dru_path)
        return {
            "dru_path": str(dru_path),
            "exists": dru_path.is_file(),
            "rules": rules,
            "total": len(rules),
        }

    @mcp.tool()
    def remove_drc_rule(name: str) -> dict:
        """Remove a custom DRC rule by name."""
        proj = state.get_active()
        dru_path = proj.path / f"{proj.name}.kicad_dru"
        rules = drc_rules.read_rules(dru_path)
        before = len(rules)
        rules = [r for r in rules if r.get("name") != name]
        if len(rules) == before:
            raise KeyError(f"no DRC rule named {name!r}")
        drc_rules.write_rules(dru_path, rules)
        return {"removed": name, "remaining": len(rules)}

    @mcp.tool()
    def clear_drc_rules() -> dict:
        """Remove every custom DRC rule (writes a `.kicad_dru` with only the version header)."""
        proj = state.get_active()
        dru_path = proj.path / f"{proj.name}.kicad_dru"
        drc_rules.write_rules(dru_path, [])
        return {"dru_path": str(dru_path), "cleared": True}

    @mcp.tool()
    def list_net_classes() -> dict:
        """List all net classes and the patterns assigned to each."""
        proj = state.get_active()
        pro = ps.load_pro(proj.pro_path)
        classes = ps.get_net_classes(pro)
        patterns = ps.get_netclass_patterns(pro)
        # Group patterns by class
        by_class: dict[str, list[str]] = {}
        for p in patterns:
            by_class.setdefault(p["netclass"], []).append(p["pattern"])
        return {
            "classes": [
                {
                    "name": c.get("name"),
                    "track_width": c.get("track_width"),
                    "clearance": c.get("clearance"),
                    "via_diameter": c.get("via_diameter"),
                    "via_drill": c.get("via_drill"),
                    "diff_pair_width": c.get("diff_pair_width"),
                    "diff_pair_gap": c.get("diff_pair_gap"),
                    "description": c.get("description", ""),
                    "patterns": by_class.get(c.get("name"), []),
                }
                for c in classes
            ],
            "total_patterns": len(patterns),
        }
