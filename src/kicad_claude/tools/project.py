"""Phase 1 — KiCAD project management tools.

Tools:
    create_project       — generate a blank KiCAD project + mark active
    set_project          — mark an existing project as active
    get_project_state    — summary of active project (counts, paths)
    list_components      — list symbols in the active schematic
"""

from __future__ import annotations

import logging
from pathlib import Path

from skip import PCB, Schematic

from kicad_claude import state
from kicad_claude.templates.blank import write_blank_project

logger = logging.getLogger("kicad-claude.tools.project")


def _resolve_project_dir_and_name(project_path: str) -> tuple[Path, str]:
    """Accept either a project directory or any of its .kicad_* files.

    Returns (directory, project_name). Raises FileNotFoundError or ValueError
    if the path is ambiguous or no project file is found.
    """
    p = Path(project_path).expanduser().resolve()
    if p.is_file():
        if p.suffix not in (".kicad_pro", ".kicad_sch", ".kicad_pcb"):
            raise ValueError(f"unsupported file extension: {p.suffix}")
        return p.parent, p.stem

    if not p.is_dir():
        raise FileNotFoundError(f"path not found: {p}")

    pros = sorted(p.glob("*.kicad_pro"))
    if not pros:
        raise FileNotFoundError(f"no .kicad_pro file in {p}")
    if len(pros) > 1:
        names = [x.name for x in pros]
        raise ValueError(
            f"multiple .kicad_pro files in {p}: {names}; pass the file path directly"
        )
    return p, pros[0].stem


def _summarize(proj: state.ActiveProject) -> dict:
    """Cheap counts of an active project: symbols, footprints, nets."""
    sch = Schematic(str(proj.sch_path))
    pcb = PCB(str(proj.pcb_path))
    return {
        "path": str(proj.path),
        "name": proj.name,
        "files": {
            "pro": str(proj.pro_path),
            "sch": str(proj.sch_path),
            "pcb": str(proj.pcb_path),
        },
        "symbols": len(sch.symbol),
        "footprints": len(getattr(pcb, "footprint", [])),
        "nets": len(pcb.net),
    }


def _component_dict(sym) -> dict:
    """Best-effort extraction of a symbol's identity. Tolerant of missing fields."""
    def _val(getter, default=None):
        try:
            return getter()
        except Exception:
            return default

    reference = _val(lambda: sym.property.Reference.value, "?")
    value = _val(lambda: sym.property.Value.value, "")
    lib_id = _val(lambda: sym.lib_id.value, "")
    at = _val(lambda: list(sym.at.value), [0.0, 0.0, 0.0])
    x = at[0] if len(at) > 0 else 0.0
    y = at[1] if len(at) > 1 else 0.0
    rotation = at[2] if len(at) > 2 else 0.0
    return {
        "reference": reference,
        "value": value,
        "lib_id": lib_id,
        "position_mm": [x, y],
        "rotation": rotation,
    }


def register(mcp) -> None:
    """Register Phase 1 tools on the FastMCP instance."""

    @mcp.tool()
    def create_project(path: str, name: str) -> dict:
        """Create a new blank KiCAD project at `path` named `name`.

        Writes `{name}.kicad_pro`, `{name}.kicad_sch`, `{name}.kicad_pcb`
        and marks the project as active. Refuses to overwrite if any of
        the three files already exists at the target.
        """
        target = Path(path).expanduser().resolve()
        files = write_blank_project(target, name)
        proj = state.set_active(target, name)
        logger.info("created project %s at %s", name, target)
        return {
            "path": str(target),
            "name": name,
            "files": {k: str(v) for k, v in files.items()},
            "active": True,
            **_summarize(proj),
        }

    @mcp.tool()
    def set_project(project_path: str) -> dict:
        """Mark an existing KiCAD project as active.

        `project_path` may be the project directory, or the path to any of
        its `.kicad_pro` / `.kicad_sch` / `.kicad_pcb` files.
        """
        directory, name = _resolve_project_dir_and_name(project_path)
        proj = state.set_active(directory, name)
        logger.info("active project: %s at %s", name, directory)
        return {"active": True, **_summarize(proj)}

    @mcp.tool()
    def get_project_state() -> dict:
        """Return a summary of the currently active KiCAD project.

        Includes paths and counts of symbols, footprints, nets.
        """
        return _summarize(state.get_active())

    @mcp.tool()
    def list_components() -> list[dict]:
        """List schematic symbols in the active project.

        Each entry: reference, value, lib_id, position_mm [x, y], rotation.
        """
        proj = state.get_active()
        sch = Schematic(str(proj.sch_path))
        return [_component_dict(sym) for sym in sch.symbol]
