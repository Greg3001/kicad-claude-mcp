"""Phase 16 — project-local libraries are placeable without re-indexing."""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_claude.adapters import sch_editor as ed
from kicad_claude.adapters import sch_io
from kicad_claude.adapters import vendor_import as vi
from kicad_claude.indexer import project_libs

from .carrier_support import call, erc_counts, needs_kicad

PINS = [  # explicit coordinates: these tests are about libraries, not layout
    {"number": str(i), "name": f"P{i}", "x_mm": -7.62, "y_mm": 5.08 - 2.54 * i,
     "angle_deg": 0, "type": "passive"}
    for i in range(1, 5)
]


def make_lib(server, lib="som", name="K26"):
    return call(server, "create_symbol", lib_name=lib, symbol_name=name, pins=PINS,
                reference_prefix="J")


def test_created_symbol_is_placeable_without_index_refresh(mcp, blank_project):
    make_lib(mcp)
    res = call(mcp, "add_symbol", lib_id="som:K26", reference="J1", value="K26",
               x_mm=76.2, y_mm=101.6)
    assert res["pin_count"] == 4
    tree = sch_io.parse_file(blank_project["sch"])
    assert ed.find_lib_symbol_def(tree, "som:K26") is not None


def test_created_symbol_is_searchable(mcp):
    make_lib(mcp, name="K26_CARRIER_CONN")
    hits = call(mcp, "search_symbol", query="K26_CARRIER_CONN")
    assert hits and hits[0]["lib_id"] == "som:K26_CARRIER_CONN"


def test_get_symbol_details_lists_project_symbol_pins(mcp):
    make_lib(mcp)
    details = call(mcp, "get_symbol_details", lib_id="som:K26")
    assert [p["number"] for p in details["pins"]] == ["1", "2", "3", "4"]


def test_symbol_added_later_appears_after_lib_changes(mcp):
    make_lib(mcp, name="A")
    assert call(mcp, "search_symbol", query="som:A")
    make_lib(mcp, name="B_LATE")
    assert call(mcp, "search_symbol", query="B_LATE")[0]["lib_id"] == "som:B_LATE"


def test_project_symbol_overrides_global_lib_id(mcp):
    # MiniLib:Resistor exists globally with 2 pins; a project lib of the same
    # name shadows it, as KiCad's own table order does.
    call(mcp, "create_symbol", lib_name="MiniLib", symbol_name="Resistor", pins=PINS)
    assert call(mcp, "get_symbol_details", lib_id="MiniLib:Resistor")["pin_count"] == 4


def test_lib_registered_under_other_name_resolves_via_table(mcp, blank_project):
    proj = blank_project["sch"].parent
    make_lib(mcp, lib="som")
    (proj / "lib" / "som.kicad_sym").rename(proj / "lib" / "file_on_disk.kicad_sym")
    (proj / "sym-lib-table").unlink()
    table = vi._read_lib_table(proj / "sym-lib-table", "sym_lib_table")
    vi._add_lib_entry(table, name="alias_name", uri="${KIPRJMOD}/lib/file_on_disk.kicad_sym")
    sch_io.write_file(proj / "sym-lib-table", table)
    libs = project_libs.symbol_libs(proj)
    assert libs["alias_name"].name == "file_on_disk.kicad_sym"
    res = call(mcp, "add_symbol", lib_id="alias_name:K26", reference="J1", value="K26",
               x_mm=76.2, y_mm=101.6)
    assert res["pin_count"] == 4


def test_unresolvable_table_entry_is_ignored(tmp_path: Path):
    (tmp_path / "sym-lib-table").write_text(
        '(sym_lib_table (version 7) (lib (name "x") (type "KiCad")'
        ' (uri "${KICAD10_SYMBOL_DIR}/x.kicad_sym") (options "") (descr "")))'
    )
    assert project_libs.symbol_libs(tmp_path) == {}


def test_no_project_libs_means_no_overlay(tmp_path: Path):
    assert project_libs.overlay_for(tmp_path) is None


def test_footprint_library_overlay_carries_path(tmp_path: Path):
    pretty = tmp_path / "lib" / "mine.pretty"
    pretty.mkdir(parents=True)
    (pretty / "FP1.kicad_mod").write_text('(footprint "FP1" (descr "d") (tags "t"))')
    overlay = project_libs.overlay_for(tmp_path)
    entry = overlay["footprints"]["mine:FP1"]
    assert Path(entry["path"]).name == "FP1.kicad_mod"


def test_unknown_symbol_still_reports_clearly(mcp):
    with pytest.raises(KeyError, match="unknown lib_id"):
        call(mcp, "add_symbol", lib_id="som:NOPE", reference="J1", value="x",
             x_mm=76.2, y_mm=101.6)


# ===== vendor imports ======================================================== #


def vendor_zip(tmp_path: Path, datasheet: str) -> Path:
    import zipfile

    path = tmp_path / "ACME.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("KiCad/ACME.kicad_sym", f"""(kicad_symbol_lib (version 20251024) (generator "snapeda")
(symbol "ACME"
  (property "Reference" "U" (at 0 0 0) (effects (font (size 1.27 1.27))))
  (property "Value" "ACME" (at 0 0 0) (effects (font (size 1.27 1.27))))
  (property "Footprint" "" (at 0 0 0) (hide yes) (effects (font (size 1.27 1.27))))
  (property "Datasheet" "{datasheet}" (at 0 0 0) (hide yes) (effects (font (size 1.27 1.27))))
  (property "Description" "" (at 0 0 0) (hide yes) (effects (font (size 1.27 1.27))))
  (symbol "ACME_1_1"
    (pin passive line (at -7.62 0 0) (length 2.54)
      (name "A" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
  )
))""")
    return path


def test_vendor_import_turns_tilde_datasheet_into_empty(tmp_path: Path, blank_project):
    proj = blank_project["sch"].parent
    vi.import_zip(vendor_zip(tmp_path, "~"), proj, target_lib="vendor")
    lib = sch_io.parse_file(proj / "lib" / "vendor.kicad_sym")
    sym_node = sch_io.find_child(lib, "symbol")
    assert ed.get_symbol_property(sym_node, "Datasheet") == ""


def test_vendor_import_keeps_real_datasheet_urls(tmp_path: Path, blank_project):
    proj = blank_project["sch"].parent
    vi.import_zip(vendor_zip(tmp_path, "https://example.com/a.pdf"), proj, target_lib="vendor")
    sym_node = sch_io.find_child(sch_io.parse_file(proj / "lib" / "vendor.kicad_sym"), "symbol")
    assert ed.get_symbol_property(sym_node, "Datasheet") == "https://example.com/a.pdf"


@needs_kicad
def test_imported_symbol_matches_library_copy_in_kicad(tmp_path: Path, mcp, blank_project):
    vi.import_zip(vendor_zip(tmp_path, "~"), blank_project["sch"].parent, target_lib="vendor")
    call(mcp, "add_symbol", lib_id="vendor:ACME", reference="U1", value="ACME",
         x_mm=101.6, y_mm=101.6)
    assert erc_counts(blank_project["sch"])["lib_symbol_mismatch"] == 0
