"""The project's sheet hierarchy, read from the files.

Why this exists: sheets can nest (a sub-sheet inside a sub-sheet), and KiCad
identifies an item's place by the instance path `/<root uuid>/<sheet uuid>/...`.
Every tool that touched sheets looked only at the root's direct children; this
module walks the whole tree once and answers: which sheets exist, who is whose
parent, and what instance path belongs to each.

A sheet file may be instantiated once only (the same file under two sheet
blocks is a valid KiCad pattern, but the tools here address sheets by file).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from kicad_claude.adapters import sch_editor as ed
from kicad_claude.adapters import sch_io

MAX_DEPTH = 8  # guards against a sheet that (indirectly) contains itself


@dataclass(frozen=True)
class SheetRef:
    name: str
    filename: str
    uuid: str
    parent: str | None  # parent's filename; None when the parent is the root
    uuids: tuple[str, ...]  # sheet uuids from the root's child down to this one
    names: tuple[str, ...]  # sheet names from the root's child down to this one

    @property
    def path(self) -> str:
        """`a/b` style address: names from the top."""
        return "/".join(self.names)


def walk(project_dir: Path, root_path: Path) -> list[SheetRef]:
    """Every sheet below the root, parents before children."""
    out: list[SheetRef] = []

    def visit(tree: list, parent: str | None, uuids: tuple, names: tuple, seen: tuple) -> None:
        if len(names) >= MAX_DEPTH:
            raise ValueError(f"sheet nesting deeper than {MAX_DEPTH} (a sheet containing itself?)")
        for node in sch_io.find_children(tree, "sheet"):
            filename = ed.get_sheet_filename(node)
            if not filename:
                continue
            if filename in seen:
                raise ValueError(f"sheet file {filename!r} contains itself")
            ref = SheetRef(
                name=_sheet_name(node), filename=filename, uuid=ed.get_sheet_uuid(node),
                parent=parent, uuids=(*uuids, ed.get_sheet_uuid(node)),
                names=(*names, _sheet_name(node)),
            )
            out.append(ref)
            child_path = Path(project_dir) / filename
            if child_path.is_file():
                visit(sch_io.parse_file(child_path), filename, ref.uuids, ref.names, (*seen, filename))

    visit(sch_io.parse_file(root_path), None, (), (), ())
    return out


def _sheet_name(node: list) -> str:
    for prop in sch_io.find_children(node, "property"):
        if len(prop) >= 3 and prop[1] == "Sheetname":
            return str(prop[2])
    return ""


def find(sheets: list[SheetRef], key: str) -> SheetRef:
    """Look a sheet up by filename, by name, or by `a/b` path; ambiguity is an error."""
    key = key.strip("/")
    hits = [s for s in sheets if s.filename == key]
    hits = hits or [s for s in sheets if s.path == key]
    hits = hits or [s for s in sheets if s.name == key]
    if not hits:
        raise KeyError(f"no sheet named {key!r}")
    if len(hits) > 1:
        raise ValueError(
            f"{key!r} is ambiguous: {[s.path for s in hits]}; use the `a/b` path or the filename"
        )
    return hits[0]


def instance_path(root_uuid: str, ref: SheetRef | None) -> str:
    """`/<root>` for the root, `/<root>/<uuid>/...` for a sheet."""
    return "/" + "/".join([root_uuid, *(ref.uuids if ref else ())])


def parent_file(project_dir: Path, root_path: Path, ref: SheetRef) -> Path:
    """The schematic file that holds `ref`'s sheet block."""
    return Path(project_dir) / ref.parent if ref.parent else Path(root_path)


def parent_instance_path(root_uuid: str, ref: SheetRef) -> str:
    """Instance path of the sheet that holds `ref`'s block (where its pins live)."""
    return "/" + "/".join([root_uuid, *ref.uuids[:-1]])
