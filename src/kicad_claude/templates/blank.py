"""Generate blank KiCAD project files programmatically.

Format versions target KiCAD 9.0+ / 10.0 (forward compatible). The
schematic and PCB templates are stored as fixture files in
`src/kicad_claude/templates/blank/`; the project file is built from a
dict because it is small and JSON-typed.
"""

from __future__ import annotations

import json
import uuid
from importlib import resources
from pathlib import Path
from string import Template


def _load_fixture(name: str) -> str:
    """Read a packaged template fixture as text."""
    return (
        resources.files("kicad_claude.templates").joinpath("blank", name).read_text()
    )


def _blank_pro(name: str) -> dict:
    """Construct a minimal .kicad_pro JSON document for project `name`."""
    return {
        "board": {
            "design_settings": {
                "defaults": {},
                "diff_pair_dimensions": [],
                "drc_exclusions": [],
                "rules": {},
                "track_widths": [],
                "via_dimensions": [],
            }
        },
        "boards": [],
        "libraries": {
            "pinned_footprint_libs": [],
            "pinned_symbol_libs": [],
        },
        "meta": {
            "filename": f"{name}.kicad_pro",
            "version": 3,
        },
        "net_settings": {
            "classes": [],
            "meta": {"version": 0},
        },
        "pcbnew": {"page_layout_descr_file": ""},
        "schematic": {},
        "sheets": [],
        "text_variables": {},
    }


def write_blank_schematic(target_path: Path) -> Path:
    """Write a single blank `.kicad_sch` to `target_path` (no .kicad_pcb / .kicad_pro).

    Useful for creating child schematics in hierarchical projects.
    Raises FileExistsError if the path already exists.
    """
    target_path = Path(target_path)
    if target_path.exists():
        raise FileExistsError(f"{target_path} already exists")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    sch_uuid = str(uuid.uuid4())
    sch_text = Template(_load_fixture("blank.kicad_sch")).substitute(SCH_UUID=sch_uuid)
    target_path.write_text(sch_text)
    return target_path


def write_blank_project(target_dir: Path, name: str) -> dict[str, Path]:
    """Create `{target_dir}/{name}.kicad_{pro,sch,pcb}` from blank templates.

    Returns a dict with keys 'pro', 'sch', 'pcb' mapping to the written paths.
    Raises FileExistsError if any of the three files already exists.
    """
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    pro_path = target_dir / f"{name}.kicad_pro"
    sch_path = target_dir / f"{name}.kicad_sch"
    pcb_path = target_dir / f"{name}.kicad_pcb"

    for p in (pro_path, sch_path, pcb_path):
        if p.exists():
            raise FileExistsError(f"{p} already exists; refusing to overwrite")

    sch_uuid = str(uuid.uuid4())
    sch_text = Template(_load_fixture("blank.kicad_sch")).substitute(SCH_UUID=sch_uuid)
    pcb_text = _load_fixture("blank.kicad_pcb")

    pro_path.write_text(json.dumps(_blank_pro(name), indent=2) + "\n")
    sch_path.write_text(sch_text)
    pcb_path.write_text(pcb_text)

    return {"pro": pro_path, "sch": sch_path, "pcb": pcb_path}
