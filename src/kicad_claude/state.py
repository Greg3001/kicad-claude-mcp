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


class NoActiveProjectError(RuntimeError):
    """Raised when a tool needs a project but none has been set."""


def set_active(path: Path | str, name: str) -> ActiveProject:
    """Mark a project as active. Validates that the three files exist."""
    global _active
    project = ActiveProject(path=Path(path).resolve(), name=name)
    project.validate()
    _active = project
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
    global _active
    _active = None
