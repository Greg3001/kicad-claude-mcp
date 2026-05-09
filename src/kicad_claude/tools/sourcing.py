"""Phase 4 — external sourcing tools.

Tools:
    check_availability    — DigiKey + Mouser stock/price lookup by MPN
    find_or_fetch_symbol  — local index → KiCAD official → manual SnapEDA fallback
    import_vendor_zip     — extract a vendor ZIP into the active project's lib/
    list_vendor_parts     — list ZIPs available under ./vendor_parts/
"""

from __future__ import annotations

import logging
from pathlib import Path

from kicad_claude import state
from kicad_claude.adapters import digikey, mouser, snapeda, vendor_import
from kicad_claude.tools import library as lib_tools

logger = logging.getLogger("kicad-claude.tools.sourcing")

VENDOR_PARTS_DIR_NAME = "vendor_parts"


def _project_root_for_vendor_parts() -> Path:
    """Return the directory where vendor ZIPs live.

    Resolution order:
    1. Active project's `vendor_parts/` (if a project is set)
    2. Repo-root's `vendor_parts/` (server.py's parent)
    """
    proj = state.get_active_or_none()
    if proj and (proj.path / VENDOR_PARTS_DIR_NAME).exists():
        return proj.path / VENDOR_PARTS_DIR_NAME

    repo_root = Path(__file__).resolve().parents[3]
    return repo_root / VENDOR_PARTS_DIR_NAME


def register(mcp) -> None:
    """Register Phase 4 tools on the FastMCP instance."""

    @mcp.tool()
    def check_availability(mpn: str) -> dict:
        """Look up `mpn` on DigiKey and Mouser. Returns stock/price/links from each.

        Either source may fail (auth, network, no match) — failures are
        captured per-source so the caller still sees what worked.
        """
        out: dict = {"mpn": mpn, "digikey": None, "mouser": None, "errors": {}}

        try:
            dk = digikey.search_keyword(mpn, limit=3)
            # Prefer an exact MPN match if present, else the first result.
            exact = next(
                (r for r in dk if (r.get("mpn") or "").upper() == mpn.upper()), None
            )
            out["digikey"] = exact or (dk[0] if dk else None)
        except digikey.DigiKeyError as e:
            out["errors"]["digikey"] = str(e)

        try:
            mo = mouser.search_part(mpn)
            exact = next(
                (r for r in mo if (r.get("mpn") or "").upper() == mpn.upper()), None
            )
            out["mouser"] = exact or (mo[0] if mo else None)
        except mouser.MouserError as e:
            out["errors"]["mouser"] = str(e)

        return out

    @mcp.tool()
    def find_or_fetch_symbol(query: str, mpn: str | None = None) -> dict:
        """Locate a KiCAD lib_id for `query`. Cascade:

        1. Local + KiCAD official libs (via the indexer's fuzzy search)
        2. If `mpn` is given and not found locally, surface manufacturer info
           from DigiKey (so the caller knows what to download from SnapEDA)
        3. Returns a clear manual-import message if all else fails.

        This tool does NOT auto-download from SnapEDA (their public site
        requires login). The caller drops a ZIP into `vendor_parts/` and
        calls `import_vendor_zip`.
        """
        # 1. Local index search
        try:
            results = lib_tools._ensure_index()
        except RuntimeError:
            return {
                "found": False,
                "hint": "Library index not built. Call `index_libraries` first.",
            }

        from kicad_claude.indexer.search import search_symbols

        hits = search_symbols(query, results, max_results=5)
        if hits and hits[0]["_score"] >= 70:
            best = hits[0]
            return {
                "found": True,
                "source": "local_index",
                "lib_id": best["lib_id"],
                "description": best["description"],
                "pin_count": best["pin_count"],
                "default_footprint": best["default_footprint"],
                "alternatives": [h["lib_id"] for h in hits[1:]],
            }

        # 2. Try to enrich with manufacturer info from DigiKey
        manufacturer = None
        if mpn:
            try:
                dk = digikey.search_keyword(mpn, limit=1)
                if dk:
                    manufacturer = dk[0].get("manufacturer")
            except digikey.DigiKeyError as e:
                logger.info("DigiKey enrichment skipped: %s", e)

        # 3. Manual fallback
        return {
            "found": False,
            "query": query,
            "mpn": mpn,
            "best_local_match": (
                {"lib_id": hits[0]["lib_id"], "score": hits[0]["_score"]}
                if hits
                else None
            ),
            "manufacturer": manufacturer,
            "instructions": snapeda.manual_fallback_message(
                mpn or query,
                manufacturer,
                vendor_parts_dir=str(_project_root_for_vendor_parts()),
            ),
            "snapeda_url": snapeda.part_page_url(mpn or query, manufacturer),
        }

    @mcp.tool()
    def import_vendor_zip(zip_path: str, target_lib: str = "vendor") -> dict:
        """Extract a vendor ZIP into `<active-project>/lib/{target_lib}.*`.

        Updates the project's `sym-lib-table` and `fp-lib-table` so KiCAD sees
        the new library. After import, call `index_libraries(force=True)` to
        refresh the searchable index (the local libs aren't auto-watched).
        """
        proj = state.get_active()
        result = vendor_import.import_zip(
            Path(zip_path), proj.path, target_lib=target_lib
        )
        # Hint the caller to refresh the index so search_symbol can find the new entries.
        result["next_step"] = "Call index_libraries(force=True) to make new lib_ids searchable."
        return result

    @mcp.tool()
    def list_vendor_parts() -> dict:
        """List ZIP files available under the `vendor_parts/` drop directory."""
        d = _project_root_for_vendor_parts()
        if not d.is_dir():
            return {"directory": str(d), "count": 0, "zips": []}
        zips = sorted(
            (
                {
                    "name": z.name,
                    "path": str(z),
                    "size_kb": round(z.stat().st_size / 1024, 1),
                }
                for z in d.glob("*.zip")
            ),
            key=lambda x: x["name"],
        )
        return {"directory": str(d), "count": len(zips), "zips": zips}
