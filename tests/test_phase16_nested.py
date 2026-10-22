"""Phase 16 — sub-sheets nested more than one level deep."""

from __future__ import annotations

import pytest

from kicad_claude.adapters import sch_editor as ed
from kicad_claude.adapters import sch_io, sheet_tree
from kicad_claude.tools import power_flags as flag_tools
from kicad_claude.tools import sync as sync_tools

from .carrier_support import call, erc_counts, needs_kicad, netlist_pins


@pytest.fixture
def nested(mcp, blank_project):
    """root -> som -> ddr, with a resistor on `ddr` exposing SIG upward."""
    sync_tools.register(mcp)
    flag_tools.register(mcp)
    call(mcp, "add_sheet", sheet_name="som", sheet_filename="som.kicad_sch",
         x_mm=25.4, y_mm=127, width_mm=38.1, height_mm=12.7)
    call(mcp, "add_sheet", sheet_name="ddr", sheet_filename="ddr.kicad_sch", parent="som",
         x_mm=76.2, y_mm=127, width_mm=38.1, height_mm=12.7)
    call(mcp, "set_active_sheet", sheet="som/ddr")
    call(mcp, "add_symbol", lib_id="MiniLib:Resistor", reference="R1", value="1k",
         x_mm=101.6, y_mm=101.6)
    call(mcp, "connect_pins", reference="R1", scope="hierarchical", connections=[
        {"pin": "1", "net": "SIG", "shape": "input"}, {"pin": "2", "net": "LOC", "scope": "local"}])
    call(mcp, "set_active_sheet", sheet="root")
    return mcp, blank_project["sch"].parent


def test_walk_finds_every_level_with_parents_and_paths(nested):
    _mcp, proj = nested
    sheets = sheet_tree.walk(proj, proj / "demo.kicad_sch")
    assert [(s.name, s.parent, s.path) for s in sheets] == [
        ("som", None, "som"), ("ddr", "som.kicad_sch", "som/ddr")]
    assert len(sheets[1].uuids) == 2


def test_find_by_filename_name_path_and_ambiguity(nested):
    _mcp, proj = nested
    sheets = sheet_tree.walk(proj, proj / "demo.kicad_sch")
    assert sheet_tree.find(sheets, "ddr.kicad_sch").path == "som/ddr"
    assert sheet_tree.find(sheets, "ddr").path == "som/ddr"
    assert sheet_tree.find(sheets, "/som/ddr/").path == "som/ddr"
    with pytest.raises(KeyError):
        sheet_tree.find(sheets, "nope")


def test_ambiguous_name_asks_for_a_path(mcp, blank_project):
    call(mcp, "add_sheet", sheet_name="a", sheet_filename="a.kicad_sch")
    call(mcp, "add_sheet", sheet_name="b", sheet_filename="b.kicad_sch")
    call(mcp, "add_sheet", sheet_name="mem", sheet_filename="a_mem.kicad_sch", parent="a")
    call(mcp, "add_sheet", sheet_name="mem", sheet_filename="b_mem.kicad_sch", parent="b")
    with pytest.raises(ValueError, match="ambiguous"):
        call(mcp, "set_active_sheet", sheet="mem")
    assert call(mcp, "set_active_sheet", sheet="b/mem")["active_sheet"] == "b_mem.kicad_sch"


def test_nested_sheet_block_lives_in_the_parent_file_with_its_instance_path(nested):
    _mcp, proj = nested
    root = sch_io.parse_file(proj / "demo.kicad_sch")
    som = sch_io.parse_file(proj / "som.kicad_sch")
    assert ed.find_sheet_by_filename(root, "ddr.kicad_sch") is None
    block = ed.find_sheet_by_filename(som, "ddr.kicad_sch")
    inst = sch_io.find_child(sch_io.find_child(block, "instances"), "project")
    path = sch_io.find_child(inst, "path")
    root_uuid = sch_io.find_child(root, "uuid")[1]
    som_uuid = ed.get_sheet_uuid(ed.find_sheet_by_filename(root, "som.kicad_sch"))
    assert path[1] == f"/{root_uuid}/{som_uuid}"


def test_pages_are_unique_across_levels(nested):
    _mcp, proj = nested
    pages = []
    for f in ("demo.kicad_sch", "som.kicad_sch"):
        for sheet in sch_io.find_children(sch_io.parse_file(proj / f), "sheet"):
            inst = sch_io.find_child(sch_io.find_child(sheet, "instances"), "project")
            pages.append(sch_io.find_child(sch_io.find_child(inst, "path"), "page")[1])
    assert pages == ["2", "3"]


def test_duplicate_file_across_levels_refused(nested):
    mcp, _ = nested
    with pytest.raises(ValueError, match="already registered"):
        call(mcp, "add_sheet", sheet_name="again", sheet_filename="ddr.kicad_sch")


def test_list_sheets_reports_every_depth(nested):
    mcp, _ = nested
    res = call(mcp, "list_sheets")
    assert res["count"] == 1 and res["total"] == 2
    assert [(s["path"], s["depth"]) for s in res["all"]] == [("som", 1), ("som/ddr", 2)]


def test_cycle_is_rejected(nested):
    _mcp, proj = nested
    ddr = sch_io.parse_file(proj / "ddr.kicad_sch")
    som = sch_io.parse_file(proj / "som.kicad_sch")
    ddr.append(ed.find_sheet_by_filename(som, "ddr.kicad_sch")[:])  # a block pointing at itself
    ddr[-1][0] = sch_io.sym("sheet")
    sch_io.write_file(proj / "ddr.kicad_sch", ddr)
    with pytest.raises(ValueError, match="contains itself"):
        sheet_tree.walk(proj, proj / "demo.kicad_sch")


@needs_kicad
def test_symbols_on_nested_sheet_get_the_right_instance_path_in_kicad(nested):
    mcp, proj = nested
    nets = netlist_pins(proj / "demo.kicad_sch")
    assert nets["/som/ddr/LOC"] == {("R1", "2")}
    assert erc_counts(proj / "demo.kicad_sch")["pin_not_connected"] >= 0  # loads without error


@needs_kicad
def test_pins_chain_through_two_levels_in_kicad(nested):
    mcp, proj = nested
    sch = proj / "demo.kicad_sch"
    # ddr -> som: pins on the block inside som.kicad_sch
    res = call(mcp, "sync_sheet_pins", sheet_name="som/ddr")
    assert res["added"] == ["SIG"]
    som_tree = sch_io.parse_file(proj / "som.kicad_sch")
    assert len(sch_io.find_children(ed.find_sheet_by_filename(som_tree, "ddr.kicad_sch"), "pin")) == 1
    # inside som: relay SIG upward with a hierarchical label on a stub
    call(mcp, "set_active_sheet", sheet="som")
    call(mcp, "label_sheet_pins", sheet_name="som/ddr", scope="local")
    call(mcp, "set_active_sheet", sheet="root")
    nets = netlist_pins(sch)
    assert nets["/som/SIG"] == {("R1", "1")}  # one net spans ddr's label and som's stub


@needs_kicad
def test_power_flag_and_annotation_work_on_nested_sheets(nested):
    mcp, proj = nested
    call(mcp, "set_active_sheet", sheet="som/ddr")
    call(mcp, "add_symbol", lib_id="MiniLib:Resistor", reference="R?", value="1k",
         x_mm=152.4, y_mm=101.6)
    px, py = [p["position_mm"] for p in call(mcp, "list_pins", reference="R1")
              if p["number"] == "2"][0]
    call(mcp, "add_power_symbol", net="GND", x_mm=px, y_mm=py)
    call(mcp, "set_active_sheet", sheet="root")
    assert call(mcp, "annotate_schematic")["total_assignments"] == 1
    res = call(mcp, "add_power_flags")
    assert res["flags_placed"] == 1 and res["flags"][0]["sheet"] == "ddr.kicad_sch"
    assert res["undriven_after"] == 0
