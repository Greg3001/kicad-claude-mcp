"""Parse KiCAD `.kicad_sym` and `.pretty/` libraries into a flat searchable index.

Output shape (saved as JSON to ~/.cache/kicad-claude/index.json):

{
    "version": 1,
    "indexed_at": "<iso-timestamp>",
    "symbol_dirs": ["..."],
    "footprint_dirs": ["..."],
    "symbols": {
        "Device:R": {
            "lib_id": "Device:R",
            "lib": "Device",
            "name": "R",
            "description": "Resistor",
            "keywords": "R res resistor",
            "default_footprint": "",
            "datasheet": "~",
            "pin_count": 2,
            "extends": null
        },
        ...
    },
    "footprints": {
        "Resistor_SMD:R_0603_1608Metric": {
            "lib_id": "Resistor_SMD:R_0603_1608Metric",
            "lib": "Resistor_SMD",
            "name": "R_0603_1608Metric",
            "description": "Resistor SMD 0603 ...",
            "tags": "resistor"
        },
        ...
    }
}
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import sexpdata

from kicad_claude.utils.kicad_paths import (
    cache_dir,
    find_footprint_lib_dirs,
    find_symbol_lib_dirs,
)

logger = logging.getLogger("kicad-claude.indexer")

INDEX_VERSION = 1


def cache_path() -> Path:
    return cache_dir() / "index.json"


# --------------------------------------------------------------------------- #
# Symbol parsing
# --------------------------------------------------------------------------- #


def _is_call(node: Any, head: str) -> bool:
    """True if `node` looks like `(head ...)` — a list whose first element is the symbol `head`."""
    return (
        isinstance(node, list)
        and len(node) > 0
        and isinstance(node[0], sexpdata.Symbol)
        and str(node[0]) == head
    )


def _head(node: Any) -> str | None:
    if isinstance(node, list) and node and isinstance(node[0], sexpdata.Symbol):
        return str(node[0])
    return None


def _count_pins(node: list) -> int:
    """Count `(pin ...)` nodes anywhere inside `node`'s direct or sub-symbol children.

    Sub-symbol units (`(symbol "Foo_0_1" ...)`) carry the actual pins.
    """
    count = 0
    for child in node[2:]:  # skip head + name
        h = _head(child)
        if h == "pin":
            count += 1
        elif h == "symbol":
            count += _count_pins(child)
    return count


def _parse_symbol(node: list, lib_name: str) -> dict[str, Any]:
    """Parse a top-level `(symbol "Name" ...)` node into our dict shape."""
    name = node[1] if len(node) > 1 and isinstance(node[1], str) else "?"
    props: dict[str, str] = {}
    extends: str | None = None

    for child in node[2:]:
        h = _head(child)
        if h == "property" and len(child) >= 3:
            # (property "Name" "Value" (...))
            pname, pval = child[1], child[2]
            if isinstance(pname, str) and isinstance(pval, str):
                props[pname] = pval
        elif h == "extends" and len(child) >= 2 and isinstance(child[1], str):
            extends = child[1]

    return {
        "lib_id": f"{lib_name}:{name}",
        "lib": lib_name,
        "name": name,
        "description": props.get("Description", ""),
        "keywords": props.get("ki_keywords", ""),
        "default_footprint": props.get("Footprint", ""),
        "datasheet": props.get("Datasheet", ""),
        "pin_count": _count_pins(node),
        "extends": extends,
    }


def _extract_pins(node: list) -> list[dict[str, Any]]:
    """Recursively gather pin metadata from a `(symbol ...)` node.

    Pins live on sub-symbol units in multi-unit symbols, so we recurse.
    """
    pins: list[dict[str, Any]] = []
    for child in node[2:]:
        h = _head(child)
        if h == "pin":
            etype = ""
            shape = ""
            if len(child) > 1 and isinstance(child[1], sexpdata.Symbol):
                etype = str(child[1])
            if len(child) > 2 and isinstance(child[2], sexpdata.Symbol):
                shape = str(child[2])
            number = ""
            pname = ""
            for sub in child[3:]:
                hs = _head(sub)
                if hs == "name" and len(sub) >= 2 and isinstance(sub[1], str):
                    pname = sub[1]
                elif hs == "number" and len(sub) >= 2 and isinstance(sub[1], str):
                    number = sub[1]
            pins.append(
                {
                    "number": number,
                    "name": pname,
                    "type": etype,
                    "shape": shape,
                }
            )
        elif h == "symbol":
            pins.extend(_extract_pins(child))
    return pins


def get_symbol_pins(lib_path: Path, symbol_name: str) -> list[dict[str, Any]]:
    """Open a `.kicad_sym` and return full pin metadata for one symbol."""
    text = lib_path.read_text(encoding="utf-8", errors="replace")
    try:
        data = sexpdata.loads(text)
    except Exception as e:  # noqa: BLE001
        logger.warning("failed to parse %s for pins: %s", lib_path, e)
        return []
    if not _is_call(data, "kicad_symbol_lib"):
        return []
    for child in data[1:]:
        if _is_call(child, "symbol") and len(child) >= 2 and child[1] == symbol_name:
            return _extract_pins(child)
    return []


def parse_symbol_lib(path: Path) -> list[dict[str, Any]]:
    """Parse a single `.kicad_sym` file into a list of symbol dicts.

    Resolves `extends` within the same library: an extending symbol inherits
    `pin_count` from its base when it has none of its own.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    try:
        data = sexpdata.loads(text)
    except Exception as e:  # noqa: BLE001 — broad on purpose, log and skip
        logger.warning("failed to parse %s: %s", path, e)
        return []

    if not _is_call(data, "kicad_symbol_lib"):
        logger.warning("not a kicad_symbol_lib: %s", path)
        return []

    lib_name = path.stem
    symbols = [
        _parse_symbol(child, lib_name)
        for child in data[1:]
        if _is_call(child, "symbol")
    ]

    # Resolve extends: inherit pin_count from base if missing
    by_name = {s["name"]: s for s in symbols}
    for s in symbols:
        base_name = s.get("extends")
        if base_name and not s["pin_count"]:
            base = by_name.get(base_name)
            if base:
                s["pin_count"] = base["pin_count"]

    return symbols


# --------------------------------------------------------------------------- #
# Footprint parsing (lightweight: header regex, no full s-expr)
# --------------------------------------------------------------------------- #

_FP_DESCR_RE = re.compile(r'^\s*\(descr\s+"((?:[^"\\]|\\.)*)"', re.MULTILINE)
_FP_TAGS_RE = re.compile(r'^\s*\(tags\s+"((?:[^"\\]|\\.)*)"', re.MULTILINE)


def parse_footprint(path: Path, lib_name: str) -> dict[str, Any]:
    """Extract minimal metadata from a `.kicad_mod` file.

    Reads only the header (~50 lines) and uses regex; full s-expression parse
    is unnecessary for indexing.
    """
    name = path.stem
    head = ""
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            head = "".join(next(f, "") for _ in range(50))
    except OSError as e:
        logger.warning("failed to read %s: %s", path, e)

    descr = ""
    tags = ""
    m = _FP_DESCR_RE.search(head)
    if m:
        descr = m.group(1)
    m = _FP_TAGS_RE.search(head)
    if m:
        tags = m.group(1)

    return {
        "lib_id": f"{lib_name}:{name}",
        "lib": lib_name,
        "name": name,
        "description": descr,
        "tags": tags,
    }


# --------------------------------------------------------------------------- #
# Full index
# --------------------------------------------------------------------------- #


def build_index(
    symbol_dirs: list[Path] | None = None,
    footprint_dirs: list[Path] | None = None,
) -> dict[str, Any]:
    """Walk the configured library directories, parse, and return the index.

    Does not write to disk; call `save_cache(idx)` for that.
    """
    sdirs = symbol_dirs if symbol_dirs is not None else find_symbol_lib_dirs()
    fdirs = footprint_dirs if footprint_dirs is not None else find_footprint_lib_dirs()

    symbols: dict[str, Any] = {}
    for d in sdirs:
        for sym_file in sorted(d.glob("*.kicad_sym")):
            for sym in parse_symbol_lib(sym_file):
                symbols[sym["lib_id"]] = sym
        logger.info("indexed symbols from %s", d)

    footprints: dict[str, Any] = {}
    for d in fdirs:
        for pretty in sorted(d.glob("*.pretty")):
            if not pretty.is_dir():
                continue
            lib_name = pretty.stem
            for mod in sorted(pretty.glob("*.kicad_mod")):
                fp = parse_footprint(mod, lib_name)
                footprints[fp["lib_id"]] = fp
        logger.info("indexed footprints from %s", d)

    return {
        "version": INDEX_VERSION,
        "indexed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "symbol_dirs": [str(p) for p in sdirs],
        "footprint_dirs": [str(p) for p in fdirs],
        "symbols": symbols,
        "footprints": footprints,
    }


def save_cache(index: dict[str, Any]) -> Path:
    p = cache_path()
    p.write_text(json.dumps(index, indent=2, ensure_ascii=False))
    return p


def load_cache() -> dict[str, Any] | None:
    """Load the cached index, or return None if missing/incompatible."""
    p = cache_path()
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("failed to load index cache %s: %s", p, e)
        return None
    if data.get("version") != INDEX_VERSION:
        logger.info("index cache version mismatch (%s); rebuilding", data.get("version"))
        return None
    return data
