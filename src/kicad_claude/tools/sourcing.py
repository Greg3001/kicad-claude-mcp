"""Phase 4 + 11 — external sourcing tools.

Tools:
    check_availability        — DigiKey + Mouser stock/price lookup by MPN
    find_or_fetch_symbol      — local index → KiCAD official → manual SnapEDA fallback
    import_vendor_zip         — extract a vendor ZIP into the active project's lib/
    list_vendor_parts         — list ZIPs available under ./vendor_parts/
    enrich_bom_with_sourcing  — augment a KiCAD BOM CSV with DigiKey/Mouser data
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path

from kicad_claude import state
from kicad_claude.adapters import digikey, kicad_cli, mouser, snapeda, vendor_import
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
    def enrich_bom_with_sourcing(
        bom_path: str | None = None,
        output_path: str | None = None,
        sourcing_field: str = "Value",
        sources: str = "digikey,mouser",
        max_rows: int = 200,
    ) -> dict:
        """Augment a KiCAD BOM CSV with live DigiKey + Mouser stock and price.

        For each unique value in `sourcing_field` (default "Value", but pass
        "MPN" if your schematic carries that custom field), this queries
        DigiKey and/or Mouser and appends columns:

            dk_mpn, dk_manufacturer, dk_stock, dk_price, dk_currency, dk_url
            mo_mpn, mo_manufacturer, mo_stock, mo_price, mo_currency, mo_url

        Empty values for components that aren't real parts (e.g. "10k") are
        normal — the API returns no match and the columns stay blank.

        If `bom_path` is None, exports a fresh BOM with `kicad-cli sch export
        bom` first into `<project>/fab/`. `max_rows` caps API calls (DigiKey
        rate-limits free tier).
        """
        proj = state.get_active()

        # Resolve / generate BOM
        if bom_path is None:
            bom = proj.path / "fab" / f"{proj.name}-bom.csv"
            bom.parent.mkdir(parents=True, exist_ok=True)
            kicad_cli.export_bom(proj.sch_path, bom)
        else:
            bom = Path(bom_path).expanduser().resolve()
            if not bom.is_file():
                raise FileNotFoundError(bom)

        out = (
            Path(output_path).expanduser()
            if output_path
            else proj.path / "fab" / f"{proj.name}-bom-enriched.csv"
        )
        out.parent.mkdir(parents=True, exist_ok=True)

        srcs = [s.strip() for s in sources.split(",") if s.strip()]
        use_dk = "digikey" in srcs
        use_mo = "mouser" in srcs

        # Read BOM (KiCAD writes UTF-8 with default delimiter ',')
        with bom.open(encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            fieldnames = list(reader.fieldnames or [])

        if sourcing_field not in fieldnames:
            raise ValueError(
                f"BOM has no field {sourcing_field!r}. Columns: {fieldnames}. "
                f"Re-run export_bom with --fields including the field, or pass "
                f"sourcing_field with one of the existing columns."
            )

        # Cache lookups by query so we don't hit the API multiple times for
        # the same value.
        dk_cache: dict[str, dict] = {}
        mo_cache: dict[str, dict] = {}
        errors: dict[str, str] = {}

        new_columns = []
        if use_dk:
            new_columns += [
                "dk_mpn", "dk_manufacturer", "dk_stock",
                "dk_price", "dk_currency", "dk_url",
            ]
        if use_mo:
            new_columns += [
                "mo_mpn", "mo_manufacturer", "mo_stock",
                "mo_price", "mo_currency", "mo_url",
            ]
        for col in new_columns:
            if col not in fieldnames:
                fieldnames.append(col)

        api_calls = 0
        for row in rows[:max_rows]:
            query = (row.get(sourcing_field) or "").strip().strip('"')
            if not query:
                continue

            if use_dk and query not in dk_cache:
                try:
                    results = digikey.search_keyword(query, limit=1)
                    dk_cache[query] = results[0] if results else {}
                    api_calls += 1
                except digikey.DigiKeyError as e:
                    errors.setdefault("digikey", str(e))
                    dk_cache[query] = {}

            if use_mo and query not in mo_cache:
                try:
                    results = mouser.search_part(query)
                    mo_cache[query] = results[0] if results else {}
                    api_calls += 1
                except mouser.MouserError as e:
                    errors.setdefault("mouser", str(e))
                    mo_cache[query] = {}

            if use_dk:
                dk = dk_cache.get(query, {})
                row["dk_mpn"] = dk.get("mpn", "")
                row["dk_manufacturer"] = dk.get("manufacturer", "")
                row["dk_stock"] = dk.get("stock", "")
                row["dk_price"] = dk.get("unit_price", "")
                row["dk_currency"] = dk.get("currency", "")
                row["dk_url"] = dk.get("product_url", "")
            if use_mo:
                mo = mo_cache.get(query, {})
                row["mo_mpn"] = mo.get("mpn", "")
                row["mo_manufacturer"] = mo.get("manufacturer", "")
                row["mo_stock"] = mo.get("stock", "")
                row["mo_price"] = mo.get("unit_price", "")
                row["mo_currency"] = mo.get("currency", "")
                row["mo_url"] = mo.get("product_url", "")

        # Write the enriched CSV
        with out.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow(row)

        # Summary stats
        dk_hits = sum(1 for v in dk_cache.values() if v)
        mo_hits = sum(1 for v in mo_cache.values() if v)
        return {
            "input_bom": str(bom),
            "output_path": str(out),
            "row_count": len(rows),
            "unique_queries": max(len(dk_cache), len(mo_cache)),
            "digikey_hits": dk_hits if use_dk else None,
            "mouser_hits": mo_hits if use_mo else None,
            "api_calls": api_calls,
            "errors": errors,
        }

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
