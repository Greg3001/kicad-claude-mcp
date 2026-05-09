"""Read and write `.kicad_pro` settings — design rules, net classes, patterns.

The `.kicad_pro` file is JSON. Two areas matter for engineering work:

    board.design_settings.rules     — DRC numerical minimums
    net_settings.classes            — net classes (track widths per group)
    net_settings.netclass_patterns  — assignments of nets to classes

KiCAD reformats this file freely on save; we just keep keys we care about
and leave everything else untouched.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------- #
# Load / save
# --------------------------------------------------------------------------- #


def load_pro(pro_path: Path) -> dict:
    return json.loads(Path(pro_path).read_text(encoding="utf-8"))


def save_pro(pro_path: Path, data: dict) -> None:
    Path(pro_path).write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


# --------------------------------------------------------------------------- #
# Design rules
# --------------------------------------------------------------------------- #


def get_design_rules(pro: dict) -> dict:
    return (
        pro.setdefault("board", {})
        .setdefault("design_settings", {})
        .setdefault("rules", {})
    )


def update_design_rules(pro: dict, **rules: Any) -> dict:
    """Merge non-None rules into the design_settings.rules dict."""
    rules_dict = get_design_rules(pro)
    for k, v in rules.items():
        if v is None:
            continue
        rules_dict[k] = v
    return rules_dict


# Standard fab presets (conservative defaults that pass JLCPCB / PCBWay basic).
# Values in mm. See https://jlcpcb.com/capabilities/pcb-capabilities for source.

FAB_PRESETS: dict[str, dict] = {
    "jlcpcb_2l_default": {
        "description": "JLCPCB 2-layer cheap default (≥0.127 mm, 6/6 mil)",
        "rules": {
            "min_clearance": 0.127,
            "min_track_width": 0.127,
            "min_via_diameter": 0.45,
            "min_via_drill": 0.2,
            "min_through_hole_diameter": 0.3,
            "min_hole_clearance": 0.254,
            "min_hole_to_hole": 0.5,
            "min_silk_clearance": 0.1,
            "min_text_height": 1.0,
            "min_text_thickness": 0.15,
            "min_copper_edge_clearance": 0.3,
            "allow_blind_buried_vias": False,
            "allow_microvias": False,
        },
    },
    "jlcpcb_2l_advanced": {
        "description": "JLCPCB 2-layer fine (≥0.0762 mm / 3 mil) — costlier",
        "rules": {
            "min_clearance": 0.0762,
            "min_track_width": 0.0762,
            "min_via_diameter": 0.3,
            "min_via_drill": 0.15,
            "min_through_hole_diameter": 0.2,
            "min_hole_clearance": 0.2,
            "min_hole_to_hole": 0.3,
            "min_silk_clearance": 0.1,
            "min_text_height": 0.8,
            "min_text_thickness": 0.13,
            "min_copper_edge_clearance": 0.25,
        },
    },
    "pcbway_default": {
        "description": "PCBWay default capabilities (similar to JLCPCB default)",
        "rules": {
            "min_clearance": 0.127,
            "min_track_width": 0.127,
            "min_via_diameter": 0.45,
            "min_via_drill": 0.2,
            "min_through_hole_diameter": 0.3,
            "min_hole_clearance": 0.254,
            "min_silk_clearance": 0.15,
        },
    },
    "oshpark_4l": {
        "description": "OSH Park 4-layer process (≥0.127 mm trace, ≥0.2 mm drill)",
        "rules": {
            "min_clearance": 0.127,
            "min_track_width": 0.127,
            "min_via_diameter": 0.508,
            "min_via_drill": 0.254,
            "min_through_hole_diameter": 0.33,
            "min_hole_clearance": 0.3,
            "min_silk_clearance": 0.15,
        },
    },
    "permissive_prototype": {
        "description": "Permissive defaults for hand-soldered prototypes",
        "rules": {
            "min_clearance": 0.2,
            "min_track_width": 0.25,
            "min_via_diameter": 0.6,
            "min_via_drill": 0.3,
            "min_through_hole_diameter": 0.4,
            "min_hole_clearance": 0.4,
        },
    },
}


# --------------------------------------------------------------------------- #
# Net classes
# --------------------------------------------------------------------------- #


# KiCAD's "Default" net class shape — we mirror it for new classes.
DEFAULT_NETCLASS = {
    "bus_width": 12,
    "clearance": 0.2,
    "diff_pair_gap": 0.25,
    "diff_pair_via_gap": 0.25,
    "diff_pair_width": 0.2,
    "line_style": 0,
    "microvia_diameter": 0.3,
    "microvia_drill": 0.1,
    "pcb_color": "rgba(0, 0, 0, 0.000)",
    "schematic_color": "rgba(0, 0, 0, 0.000)",
    "track_width": 0.25,
    "via_diameter": 0.6,
    "via_drill": 0.3,
    "wire_width": 6,
}


def get_net_classes(pro: dict) -> list:
    return pro.setdefault("net_settings", {}).setdefault("classes", [])


def find_net_class(pro: dict, name: str) -> dict | None:
    for c in get_net_classes(pro):
        if c.get("name") == name:
            return c
    return None


def add_or_update_net_class(
    pro: dict,
    name: str,
    *,
    track_width_mm: float | None = None,
    clearance_mm: float | None = None,
    via_diameter_mm: float | None = None,
    via_drill_mm: float | None = None,
    diff_pair_width_mm: float | None = None,
    diff_pair_gap_mm: float | None = None,
    description: str = "",
) -> dict:
    """Add a new net class or update an existing one. Returns the class dict."""
    classes = get_net_classes(pro)
    existing = find_net_class(pro, name)
    if existing is None:
        cls: dict = {**DEFAULT_NETCLASS, "name": name}
        if description:
            cls["description"] = description
        classes.append(cls)
    else:
        cls = existing
        if description:
            cls["description"] = description

    if track_width_mm is not None:
        cls["track_width"] = float(track_width_mm)
    if clearance_mm is not None:
        cls["clearance"] = float(clearance_mm)
    if via_diameter_mm is not None:
        cls["via_diameter"] = float(via_diameter_mm)
    if via_drill_mm is not None:
        cls["via_drill"] = float(via_drill_mm)
    if diff_pair_width_mm is not None:
        cls["diff_pair_width"] = float(diff_pair_width_mm)
    if diff_pair_gap_mm is not None:
        cls["diff_pair_gap"] = float(diff_pair_gap_mm)
    return cls


def remove_net_class(pro: dict, name: str) -> bool:
    classes = get_net_classes(pro)
    for i, c in enumerate(classes):
        if c.get("name") == name:
            classes.pop(i)
            # Drop dangling pattern assignments
            patterns = pro.setdefault("net_settings", {}).setdefault(
                "netclass_patterns", []
            )
            patterns[:] = [p for p in patterns if p.get("netclass") != name]
            return True
    return False


def get_netclass_patterns(pro: dict) -> list:
    return pro.setdefault("net_settings", {}).setdefault("netclass_patterns", [])


def assign_pattern(pro: dict, *, netclass: str, pattern: str) -> dict:
    """Assign nets matching `pattern` (KiCAD glob) to `netclass`. Idempotent."""
    if find_net_class(pro, netclass) is None:
        raise KeyError(f"net class {netclass!r} doesn't exist; create it first")
    patterns = get_netclass_patterns(pro)
    entry = {"netclass": netclass, "pattern": pattern}
    for existing in patterns:
        if existing.get("netclass") == netclass and existing.get("pattern") == pattern:
            return existing
    patterns.append(entry)
    return entry
