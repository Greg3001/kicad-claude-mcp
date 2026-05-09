"""Active KiCAD project — module-level singleton.

Tools that operate on the project (add_symbol, autoroute_pcb, …) read from
this. `set_project` writes to it. The MCP server has a single active
project at a time.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ActiveProject:
    """Paths and identity of the currently active KiCAD project."""

    path: Path
    name: str

    @property
    def pro_path(self) -> Path:
        return self.path / f"{self.name}.kicad_pro"

    @property
    def sch_path(self) -> Path:
        return self.path / f"{self.name}.kicad_sch"

    @property
    def pcb_path(self) -> Path:
        return self.path / f"{self.name}.kicad_pcb"

    def validate(self) -> None:
        """Raise FileNotFoundError if any of the three project files is missing."""
        for p in (self.pro_path, self.sch_path, self.pcb_path):
            if not p.is_file():
                raise FileNotFoundError(f"missing KiCAD project file: {p}")


_active: ActiveProject | None = None
_active_sheet: str | None = None  # filename relative to proj.path; None == root
_active_board: str | None = None  # filename relative to proj.path; None == proj.pcb_path


class NoActiveProjectError(RuntimeError):
    """Raised when a tool needs a project but none has been set."""


def set_active(path: Path | str, name: str) -> ActiveProject:
    """Mark a project as active. Validates that the three files exist."""
    global _active, _active_sheet, _active_board
    project = ActiveProject(path=Path(path).resolve(), name=name)
    project.validate()
    _active = project
    _active_sheet = None  # reset to root on project switch
    _active_board = None  # reset to default board on project switch
    return project


def get_active() -> ActiveProject:
    """Return the active project. Raises if none is set."""
    if _active is None:
        raise NoActiveProjectError(
            "No active KiCAD project. Call set_project or create_project first."
        )
    return _active


def get_active_or_none() -> ActiveProject | None:
    return _active


def clear_active() -> None:
    """Reset the active project (used in tests)."""
    global _active, _active_sheet, _active_board
    _active = None
    _active_sheet = None
    _active_board = None


# --------------------------------------------------------------------------- #
# Active sheet (hierarchical schematic context)
# --------------------------------------------------------------------------- #


def set_active_sheet(filename: str | None) -> None:
    """Switch the active sub-sheet. `None` or empty string returns to the root.

    Filename is relative to the project directory (e.g. "power_supply.kicad_sch").
    """
    global _active_sheet
    if not filename:
        _active_sheet = None
        return
    if not filename.endswith(".kicad_sch"):
        raise ValueError(f"sheet filename must end with .kicad_sch (got {filename!r})")
    proj = get_active()
    if not (proj.path / filename).is_file():
        raise FileNotFoundError(f"{proj.path / filename} does not exist")
    _active_sheet = filename


def get_active_sheet_filename() -> str | None:
    """Return the active sheet's filename, or None when on the root sheet."""
    return _active_sheet


def get_active_sheet_path() -> Path:
    """Return the absolute path of the active schematic file (root or child)."""
    proj = get_active()
    if _active_sheet is None:
        return proj.sch_path
    return proj.path / _active_sheet


# --------------------------------------------------------------------------- #
# Active PCB / multi-board projects
# --------------------------------------------------------------------------- #


def set_active_board(filename: str | None) -> None:
    """Switch the active PCB. `None` or empty returns to the default `proj.pcb_path`."""
    global _active_board
    if not filename:
        _active_board = None
        return
    if not filename.endswith(".kicad_pcb"):
        raise ValueError(f"board filename must end with .kicad_pcb (got {filename!r})")
    proj = get_active()
    if not (proj.path / filename).is_file():
        raise FileNotFoundError(f"{proj.path / filename} does not exist")
    _active_board = filename


def get_active_board_filename() -> str | None:
    """Return the active board filename (None when on the project's main PCB)."""
    return _active_board


def get_active_board_path() -> Path:
    """Return the absolute path of the active .kicad_pcb."""
    proj = get_active()
    if _active_board is None:
        return proj.pcb_path
    return proj.path / _active_board
