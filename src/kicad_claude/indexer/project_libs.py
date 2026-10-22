"""Project-local symbol/footprint libraries as an overlay on the global index.

Why this exists: the global index is built once from KiCad's install and cached
in `~/.cache`. Libraries a project creates or imports (`create_symbol`,
`import_vendor_zip`) live in `<project>/lib/` and are registered in the
project's `sym-lib-table` / `fp-lib-table`, so they never reached `add_symbol`
or `add_footprint` without a manual env-var dance. This module reads the
tables (plus any unregistered `lib/*.kicad_sym` and `lib/*.pretty`) and yields
index entries carrying an explicit `path`, which placement prefers.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from kicad_claude.adapters import sch_io
from kicad_claude.indexer.kicad_libs import parse_footprint, parse_symbol_lib

logger = logging.getLogger("kicad-claude.indexer.project_libs")

_PROJECT_VAR = "${KIPRJMOD}"


def _table_entries(table_path: Path) -> dict[str, Path]:
    """{lib name: resolved path} for the entries of a lib table we can resolve."""
    if not table_path.is_file():
        return {}
    try:
        tree = sch_io.parse_file(table_path)
    except Exception:  # noqa: BLE001 - a malformed table must not break placement
        logger.warning("unreadable lib table %s", table_path)
        return {}
    out: dict[str, Path] = {}
    for lib in sch_io.find_children(tree, "lib"):
        name = sch_io.find_child(lib, "name")
        uri = sch_io.find_child(lib, "uri")
        if not (name and uri and len(name) >= 2 and len(uri) >= 2):
            continue
        raw = str(uri[1])
        if raw.startswith(_PROJECT_VAR):
            path = table_path.parent / raw[len(_PROJECT_VAR):].lstrip("/\\")
        elif raw.startswith("${"):
            continue  # some other variable (KiCad's own libs): not ours to resolve
        else:
            path = Path(raw).expanduser()
        out[str(name[1])] = path
    return out


def symbol_libs(project_dir: Path) -> dict[str, Path]:
    """Registered project symbol libs, plus unregistered `lib/*.kicad_sym`."""
    libs = {n: p for n, p in _table_entries(project_dir / "sym-lib-table").items() if p.is_file()}
    for path in sorted((project_dir / "lib").glob("*.kicad_sym")):
        libs.setdefault(path.stem, path)
    return libs


def footprint_libs(project_dir: Path) -> dict[str, Path]:
    """Registered project footprint libs, plus unregistered `lib/*.pretty`."""
    libs = {n: p for n, p in _table_entries(project_dir / "fp-lib-table").items() if p.is_dir()}
    for path in sorted((project_dir / "lib").glob("*.pretty")):
        libs.setdefault(path.stem, path)
    return libs


def _fingerprint(paths: list[Path]) -> tuple:
    """Cheap change detector: path + mtime + size of every library file."""
    out = []
    for p in paths:
        files = sorted(p.glob("*.kicad_mod")) if p.is_dir() else [p]
        out += [(str(f), f.stat().st_mtime_ns, f.stat().st_size) for f in files]
    return tuple(out)


def _symbol_entries(libs: dict[str, Path]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for lib_name, path in libs.items():
        for entry in parse_symbol_lib(path):
            lib_id = f"{lib_name}:{entry['name']}"
            out[lib_id] = {**entry, "lib": lib_name, "lib_id": lib_id, "path": str(path)}
    return out


def _footprint_entries(libs: dict[str, Path]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for lib_name, pretty in libs.items():
        for mod in sorted(pretty.glob("*.kicad_mod")):
            entry = parse_footprint(mod, lib_name)
            out[entry["lib_id"]] = {**entry, "path": str(mod)}
    return out


_cache: dict[str, tuple[tuple, dict[str, Any]]] = {}


def overlay_for(project_dir: Path) -> dict[str, Any] | None:
    """Index entries for the project's own libraries (None when it has none)."""
    sym_libs, fp_libs = symbol_libs(project_dir), footprint_libs(project_dir)
    if not sym_libs and not fp_libs:
        return None
    key = _fingerprint([*sym_libs.values(), *fp_libs.values()])
    hit = _cache.get(str(project_dir))
    if hit is not None and hit[0] == key:
        return hit[1]
    overlay = {
        "key": key,
        "symbols": _symbol_entries(sym_libs),
        "footprints": _footprint_entries(fp_libs),
    }
    _cache[str(project_dir)] = (key, overlay)
    return overlay
