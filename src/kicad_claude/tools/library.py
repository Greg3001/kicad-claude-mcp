"""Phase 2 — KiCAD library indexing and search tools.

Tools:
    index_libraries      — build (or load cached) index of KiCAD libs
    list_libraries       — list indexed symbol/footprint libraries with counts
    search_symbol        — fuzzy search across indexed symbols
    search_footprint     — fuzzy search across indexed footprints
    get_symbol_details   — full metadata + pin list for a specific symbol
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from kicad_claude.indexer.kicad_libs import (
    build_index,
    cache_path,
    get_symbol_pins,
    load_cache,
    save_cache,
)
from kicad_claude.indexer.search import search_footprints, search_symbols

logger = logging.getLogger("kicad-claude.tools.library")

# In-process memoization of the loaded index. The on-disk cache is the source
# of truth; this avoids re-reading the multi-MB JSON on every tool call.
_index: dict[str, Any] | None = None


def _ensure_index() -> dict[str, Any]:
    global _index
    if _index is None:
        _index = load_cache()
        if _index is None:
            raise RuntimeError(
                "Library index not built. Call `index_libraries` first."
            )
    return _index


def _invalidate() -> None:
    global _index
    _index = None


def _summary(index: dict[str, Any], from_cache: bool) -> dict[str, Any]:
    sym_libs = {s["lib"] for s in index["symbols"].values()}
    fp_libs = {f["lib"] for f in index["footprints"].values()}
    return {
        "from_cache": from_cache,
        "indexed_at": index.get("indexed_at"),
        "cache_path": str(cache_path()),
        "symbol_libraries": len(sym_libs),
        "symbols": len(index["symbols"]),
        "footprint_libraries": len(fp_libs),
        "footprints": len(index["footprints"]),
        "symbol_dirs": index.get("symbol_dirs", []),
        "footprint_dirs": index.get("footprint_dirs", []),
    }


def register(mcp) -> None:
    """Register Phase 2 tools on the FastMCP instance."""

    @mcp.tool()
    def index_libraries(force: bool = False) -> dict:
        """Build or refresh the KiCAD library index.

        On first run (or when `force=True`), walks the configured KiCAD
        symbol/footprint directories, parses every `.kicad_sym` and reads the
        header of every `.kicad_mod`, and writes a cache to
        `~/.cache/kicad-claude/index.json`.

        Subsequent calls return the cached summary instantly.
        """
        global _index
        if not force:
            cached = load_cache()
            if cached is not None:
                _index = cached
                return _summary(cached, from_cache=True)

        logger.info("building library index from scratch")
        idx = build_index()
        save_cache(idx)
        _index = idx
        return _summary(idx, from_cache=False)

    @mcp.tool()
    def list_libraries() -> dict:
        """List indexed libraries with per-library entry counts.

        Returns:
            symbol_libraries: list of {name, count}, sorted by name
            footprint_libraries: same shape
        """
        idx = _ensure_index()
        sym_counts: dict[str, int] = {}
        for s in idx["symbols"].values():
            sym_counts[s["lib"]] = sym_counts.get(s["lib"], 0) + 1
        fp_counts: dict[str, int] = {}
        for f in idx["footprints"].values():
            fp_counts[f["lib"]] = fp_counts.get(f["lib"], 0) + 1

        return {
            "symbol_libraries": sorted(
                [{"name": k, "count": v} for k, v in sym_counts.items()],
                key=lambda x: x["name"],
            ),
            "footprint_libraries": sorted(
                [{"name": k, "count": v} for k, v in fp_counts.items()],
                key=lambda x: x["name"],
            ),
        }

    @mcp.tool()
    def search_symbol(query: str, max_results: int = 10) -> list[dict]:
        """Fuzzy search across indexed symbols by lib_id, description, keywords.

        Returns up to `max_results` ranked matches. Each entry includes
        `_score` (0–100) so the caller can judge match confidence.
        """
        idx = _ensure_index()
        return search_symbols(query, idx, max_results=max_results)

    @mcp.tool()
    def search_footprint(query: str, max_results: int = 10) -> list[dict]:
        """Fuzzy search across indexed footprints by lib_id, description, tags."""
        idx = _ensure_index()
        return search_footprints(query, idx, max_results=max_results)

    @mcp.tool()
    def get_symbol_details(lib_id: str) -> dict:
        """Return full metadata and pin list for a symbol by `lib_id`.

        Re-parses the source `.kicad_sym` to extract pins (number, name,
        electrical type, shape) — the index only stores pin counts.
        """
        idx = _ensure_index()
        meta = idx["symbols"].get(lib_id)
        if meta is None:
            raise KeyError(f"unknown symbol lib_id: {lib_id!r}")

        pins: list[dict] = []
        for d_str in idx.get("symbol_dirs", []):
            lib_path = Path(d_str) / f"{meta['lib']}.kicad_sym"
            if lib_path.is_file():
                pins = get_symbol_pins(lib_path, meta["name"])
                break

        return {**meta, "pins": pins}
