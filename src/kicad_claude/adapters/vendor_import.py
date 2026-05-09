"""Import a vendor ZIP (SnapEDA / Ultra Librarian / KiCad packs) into the
active project's lib directory and register it in the lib tables.

Layout of typical vendor ZIPs:
    {MPN}.zip
    ├── KiCad/
    │   ├── {MPN}.kicad_sym
    │   └── {MPN}.pretty/
    │       └── {MPN}.kicad_mod
    └── 3D/{MPN}.step           (optional)

The importer walks the extracted tree, copies every `.kicad_sym` and
`*.pretty/` it finds into `<project>/lib/`, merging into a single project
library named `target_lib` (default: "vendor"). The KiCAD project's
`sym-lib-table` and `fp-lib-table` are updated to register the libs.
"""

from __future__ import annotations

import logging
import re
import shutil
import tempfile
import zipfile
from pathlib import Path

from kicad_claude.adapters import sch_io
from kicad_claude.adapters.sch_io import find_children, is_call, sym
from kicad_claude.indexer.kicad_libs import parse_symbol_lib

logger = logging.getLogger("kicad-claude.adapters.vendor_import")


# --------------------------------------------------------------------------- #
# Lib-table read/write
# --------------------------------------------------------------------------- #


def _read_lib_table(path: Path, root_head: str) -> list:
    """Parse a sym-lib-table or fp-lib-table; return its tree.

    If the file doesn't exist, build a minimal one.
    """
    if path.is_file():
        try:
            return sch_io.parse_file(path)
        except Exception:  # noqa: BLE001
            logger.warning("malformed %s; rebuilding", path)
    return [sym(root_head), [sym("version"), 7]]


def _has_lib(table: list, name: str) -> bool:
    for node in find_children(table, "lib"):
        for sub in node[1:]:
            if is_call(sub, "name") and len(sub) >= 2 and sub[1] == name:
                return True
    return False


def _add_lib_entry(
    table: list,
    *,
    name: str,
    uri: str,
    lib_type: str = "KiCad",
    descr: str = "Imported by kicad-claude",
) -> None:
    if _has_lib(table, name):
        return
    table.append(
        [
            sym("lib"),
            [sym("name"), name],
            [sym("type"), lib_type],
            [sym("uri"), uri],
            [sym("options"), ""],
            [sym("descr"), descr],
        ]
    )


def update_sym_lib_table(project_dir: Path, lib_name: str) -> Path:
    path = project_dir / "sym-lib-table"
    table = _read_lib_table(path, "sym_lib_table")
    _add_lib_entry(
        table,
        name=lib_name,
        uri=f"${{KIPRJMOD}}/lib/{lib_name}.kicad_sym",
        descr=f"{lib_name} symbols (kicad-claude)",
    )
    sch_io.write_file(path, table)
    return path


def update_fp_lib_table(project_dir: Path, lib_name: str) -> Path:
    path = project_dir / "fp-lib-table"
    table = _read_lib_table(path, "fp_lib_table")
    _add_lib_entry(
        table,
        name=lib_name,
        uri=f"${{KIPRJMOD}}/lib/{lib_name}.pretty",
        descr=f"{lib_name} footprints (kicad-claude)",
    )
    sch_io.write_file(path, table)
    return path


# --------------------------------------------------------------------------- #
# Symbol lib merge
# --------------------------------------------------------------------------- #


def _merge_symbol_lib(target_path: Path, sources: list[Path]) -> int:
    """Merge symbol definitions from `sources` into `target_path`.

    Creates target if missing. Returns count of symbols added (not counting
    duplicates by name).
    """
    if target_path.is_file():
        target = sch_io.parse_file(target_path)
        if not is_call(target, "kicad_symbol_lib"):
            raise ValueError(f"target {target_path} is not a kicad_symbol_lib")
    else:
        target = [
            sym("kicad_symbol_lib"),
            [sym("version"), 20251024],
            [sym("generator"), "kicad-claude"],
            [sym("generator_version"), "0.1"],
        ]

    existing_names = {
        c[1] for c in find_children(target, "symbol") if len(c) >= 2 and isinstance(c[1], str)
    }

    added = 0
    for src in sources:
        try:
            src_tree = sch_io.parse_file(src)
        except Exception as e:  # noqa: BLE001
            logger.warning("failed to parse %s: %s", src, e)
            continue
        if not is_call(src_tree, "kicad_symbol_lib"):
            logger.warning("skipping non-kicad_symbol_lib: %s", src)
            continue
        for sym_node in find_children(src_tree, "symbol"):
            name = sym_node[1] if len(sym_node) >= 2 and isinstance(sym_node[1], str) else None
            if not name or name in existing_names:
                continue
            target.append(sym_node)
            existing_names.add(name)
            added += 1

    sch_io.write_file(target_path, target)
    return added


def _merge_pretty_dirs(target_dir: Path, sources: list[Path]) -> int:
    """Copy every *.kicad_mod from each source `.pretty/` dir into `target_dir`.

    Returns count of files added (skips duplicates by filename).
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    added = 0
    for src in sources:
        if not src.is_dir():
            continue
        for mod in src.glob("*.kicad_mod"):
            dest = target_dir / mod.name
            if dest.exists():
                continue
            shutil.copy2(mod, dest)
            added += 1
    return added


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #


def import_zip(zip_path: Path, project_dir: Path, target_lib: str = "vendor") -> dict:
    """Extract `zip_path`, merge its KiCad assets into `project_dir/lib/<target_lib>.*`.

    Updates `sym-lib-table` and `fp-lib-table` to register the lib (idempotent).
    Returns a summary of what was imported.
    """
    zip_path = Path(zip_path).expanduser().resolve()
    project_dir = Path(project_dir).expanduser().resolve()
    if not zip_path.is_file():
        raise FileNotFoundError(zip_path)

    if not re.fullmatch(r"[A-Za-z0-9_\-]+", target_lib):
        raise ValueError(
            f"target_lib must be alphanumeric (got {target_lib!r}); "
            "spaces and special chars break KiCAD's lib table"
        )

    lib_dir = project_dir / "lib"
    lib_dir.mkdir(parents=True, exist_ok=True)

    sym_target = lib_dir / f"{target_lib}.kicad_sym"
    pretty_target = lib_dir / f"{target_lib}.pretty"

    with tempfile.TemporaryDirectory(prefix="kicad-claude-vendor-") as tmpdir:
        tmp = Path(tmpdir)
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(tmp)

        sym_sources = sorted(tmp.rglob("*.kicad_sym"))
        pretty_sources = sorted(p for p in tmp.rglob("*.pretty") if p.is_dir())

        if not sym_sources and not pretty_sources:
            raise ValueError(
                f"no KiCad assets found in {zip_path.name} "
                f"(looked for *.kicad_sym and *.pretty/)"
            )

        symbols_added = _merge_symbol_lib(sym_target, sym_sources) if sym_sources else 0
        footprints_added = (
            _merge_pretty_dirs(pretty_target, pretty_sources) if pretty_sources else 0
        )

    sym_table_path = update_sym_lib_table(project_dir, target_lib) if sym_sources else None
    fp_table_path = update_fp_lib_table(project_dir, target_lib) if pretty_sources else None

    # Discover the lib_ids that were imported (for caller's convenience).
    new_lib_ids: list[str] = []
    if sym_target.is_file():
        try:
            new_lib_ids = [s["lib_id"] for s in parse_symbol_lib(sym_target)]
            new_lib_ids = [
                lid.replace(f"{sym_target.stem}:", f"{target_lib}:") for lid in new_lib_ids
            ]
        except Exception:  # noqa: BLE001
            pass

    return {
        "zip": str(zip_path),
        "target_lib": target_lib,
        "symbols_added": symbols_added,
        "footprints_added": footprints_added,
        "kicad_sym_path": str(sym_target) if sym_sources else None,
        "pretty_path": str(pretty_target) if pretty_sources else None,
        "sym_lib_table": str(sym_table_path) if sym_table_path else None,
        "fp_lib_table": str(fp_table_path) if fp_table_path else None,
        "lib_ids_in_target": new_lib_ids,
    }
