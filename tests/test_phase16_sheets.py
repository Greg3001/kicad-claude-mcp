"""Phase 16 — sheets: paper size, page numbers, failure atomicity."""

from __future__ import annotations

import pytest

from kicad_claude.adapters import sch_editor as ed
from kicad_claude.adapters import sch_io, sch_sheets

from .carrier_support import call, needs_kicad, netlist_pins


def paper_of(path) -> str:
    return sch_io.find_child(sch_io.parse_file(path), "paper")[1]


def test_set_sheet_paper_changes_header_and_flip_height(mcp, blank_project):
    res = call(mcp, "set_sheet_paper", paper="A3")
    assert res["size_mm"] == [420.0, 297.0]
    tree = sch_io.parse_file(blank_project["sch"])
    assert paper_of(blank_project["sch"]) == "A3"
    assert ed.page_height_mm(tree) == 294.64


def test_set_sheet_paper_refuses_sheet_with_content(mcp, blank_project):
    call(mcp, "add_label", net_name="X", x_mm=50.8, y_mm=50.8)
    with pytest.raises(ValueError, match="already has content"):
        call(mcp, "set_sheet_paper", paper="A3")
    assert paper_of(blank_project["sch"]) == "A4"


def test_unknown_paper_rejected(mcp):
    with pytest.raises(ValueError, match="paper must be one of"):
        call(mcp, "set_sheet_paper", paper="A9")


def test_lib_symbols_alone_is_not_content():
    tree = sch_io.parse_file_text(
        '(kicad_sch (version 20250114) (uuid "u") (paper "A4") (lib_symbols))'
    )
    assert not sch_sheets.has_content(tree)


def test_add_sheet_paper_applies_to_child(mcp, blank_project):
    call(mcp, "add_sheet", sheet_name="som", sheet_filename="som.kicad_sch", paper="A2")
    child = blank_project["sch"].parent / "som.kicad_sch"
    assert paper_of(child) == "A2"


def test_add_sheet_with_bad_paper_leaves_root_untouched(mcp, blank_project):
    before = blank_project["sch"].read_text()
    with pytest.raises(ValueError):
        call(mcp, "add_sheet", sheet_name="som", sheet_filename="som.kicad_sch", paper="A9")
    assert blank_project["sch"].read_text() == before
    assert not (blank_project["sch"].parent / "som.kicad_sch").exists()


def test_add_sheet_over_existing_file_leaves_root_untouched(mcp, blank_project):
    (blank_project["sch"].parent / "som.kicad_sch").write_text("occupied")
    before = blank_project["sch"].read_text()
    with pytest.raises(FileExistsError):
        call(mcp, "add_sheet", sheet_name="som", sheet_filename="som.kicad_sch")
    assert blank_project["sch"].read_text() == before


def test_sheets_get_distinct_page_numbers(mcp, blank_project):
    for i in range(3):
        call(mcp, "add_sheet", sheet_name=f"s{i}", sheet_filename=f"s{i}.kicad_sch",
             x_mm=25.4 + 50.8 * i, y_mm=127)
    root = sch_io.parse_file(blank_project["sch"])
    pages = []
    for sheet in sch_io.find_children(root, "sheet"):
        inst = sch_io.find_child(sch_io.find_child(sheet, "instances"), "project")
        pages.append(sch_io.find_child(sch_io.find_child(inst, "path"), "page")[1])
    assert pages == ["2", "3", "4"]


@needs_kicad
def test_a3_child_sheet_pins_inside_page_are_seen_by_kicad(mcp, blank_project):
    call(mcp, "add_sheet", sheet_name="som", sheet_filename="som.kicad_sch", paper="A3")
    call(mcp, "set_active_sheet", sheet="som")
    call(mcp, "add_symbol", lib_id="MiniLib:Resistor", reference="R1", value="1k",
         x_mm=380.0, y_mm=254.0)  # beyond A4 width (297), inside A3 (420)
    call(mcp, "add_no_connect", reference="R1", pin="1")
    call(mcp, "add_no_connect", reference="R1", pin="2")
    nets = netlist_pins(blank_project["sch"])
    assert any(("R1", "1") in pins for pins in nets.values())
