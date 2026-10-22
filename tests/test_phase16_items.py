"""Phase 16 — newline-safe strings, user paper, notes, DNP, batch items, grid warnings."""

from __future__ import annotations

import pytest

from kicad_claude.adapters import sch_editor as ed
from kicad_claude.adapters import sch_io, sch_sheets

from .carrier_support import call, erc_counts, needs_kicad, netlist_pins


# ===== D1: newlines in strings =============================================== #


@needs_kicad
def test_multiline_note_and_description_load_in_kicad(mcp, blank_project):
    call(mcp, "add_text_note", text="Power sequencing:\n1. VCC_AUX\n2. VCC_INT", x_mm=50.8, y_mm=50.8)
    call(mcp, "add_symbol", lib_id="MiniLib:Resistor", reference="R1", value="1k",
         x_mm=101.6, y_mm=101.6, fields={"Notes": "line one\nline two"})
    counts = erc_counts(blank_project["sch"])  # rc=3 / empty report if the file did not load
    assert counts["pin_not_connected"] == 2
    tree = sch_io.parse_file(blank_project["sch"])
    assert ed.get_symbol_property(ed.find_symbol_by_reference(tree, "R1"), "Notes") == "line one\nline two"


# ===== D2: user paper ======================================================== #


def test_user_paper_sets_header_and_flip_height(mcp, blank_project):
    res = call(mcp, "set_sheet_paper", paper="User", width_mm=1500, height_mm=900)
    assert res["size_mm"] == [1500.0, 900.0]
    tree = sch_io.parse_file(blank_project["sch"])
    assert ed.page_height_mm(tree) == 899.16  # 900 rounded down to the 2.54 grid


def test_user_paper_needs_dimensions():
    tree = sch_io.parse_file_text('(kicad_sch (version 20250114) (uuid "u") (paper "A4"))')
    with pytest.raises(ValueError, match="needs width_mm and height_mm"):
        sch_sheets.set_paper(tree, "User")
    with pytest.raises(ValueError, match="between 50 and 3000"):
        sch_sheets.set_paper(tree, "User", 10, 10)


def test_add_sheet_accepts_user_paper(mcp, blank_project):
    call(mcp, "add_sheet", sheet_name="som", sheet_filename="som.kicad_sch",
         paper="User", paper_width_mm=1200, paper_height_mm=800)
    child = sch_io.parse_file(blank_project["sch"].parent / "som.kicad_sch")
    assert sch_io.find_child(child, "paper")[1:] == ["User", 1200, 800]


@needs_kicad
def test_user_paper_sheet_loads_and_connects_in_kicad(mcp, blank_project):
    call(mcp, "set_sheet_paper", paper="User", width_mm=1500, height_mm=900)
    call(mcp, "add_symbol", lib_id="MiniLib:Resistor", reference="R1", value="1k",
         x_mm=1200.66, y_mm=700.04)
    call(mcp, "connect_pins", reference="R1", connections=[{"pin": "1", "net": "A"}, {"pin": "2", "net": "B"}])
    nets = netlist_pins(blank_project["sch"])
    assert nets["/A"] == {("R1", "1")} and nets["/B"] == {("R1", "2")}


# ===== D6: notes, DNP, fields at place time ================================== #


def test_set_dnp_flips_every_unit(mcp, blank_project):
    for unit, x in ((1, 76.2), (2, 152.4)):
        call(mcp, "add_symbol", lib_id="UnitLib:Quad", reference="U1", value="Q",
             x_mm=x, y_mm=101.6, unit=unit)
    assert call(mcp, "set_dnp", reference="U1")["units"] == 2
    tree = sch_io.parse_file(blank_project["sch"])
    flags = {sch_io.find_child(n, "dnp")[1] for n in ed.find_symbols_by_reference(tree, "U1")}
    assert flags == {sch_io.sym("yes")}
    call(mcp, "set_dnp", reference="U1", dnp=False)
    tree = sch_io.parse_file(blank_project["sch"])
    assert {sch_io.find_child(n, "dnp")[1] for n in ed.find_symbols_by_reference(tree, "U1")} == {sch_io.sym("no")}


def test_set_dnp_unknown_reference(mcp):
    with pytest.raises(KeyError):
        call(mcp, "set_dnp", reference="Z9")


def test_add_symbol_fields_and_footprint_at_place_time(mcp, blank_project):
    res = call(mcp, "add_symbol", lib_id="MiniLib:Resistor", reference="R1", value="1k",
               x_mm=101.6, y_mm=101.6, footprint="Resistor_SMD:R_0402_1005Metric",
               fields={"MPN": "RC0402FR-071KL", "Rad Grade": "COTS"})
    assert res["warnings"] == []
    node = ed.find_symbol_by_reference(sch_io.parse_file(blank_project["sch"]), "R1")
    assert ed.get_symbol_property(node, "MPN") == "RC0402FR-071KL"
    assert ed.get_symbol_property(node, "Rad Grade") == "COTS"
    assert ed.get_symbol_property(node, "Footprint") == "Resistor_SMD:R_0402_1005Metric"


def test_fields_reach_every_unit_of_a_multi_unit_part(mcp, blank_project):
    call(mcp, "add_symbol", lib_id="UnitLib:Quad", reference="U1", value="Q",
         x_mm=76.2, y_mm=101.6, unit=1)
    call(mcp, "add_symbol", lib_id="UnitLib:Quad", reference="U1", value="Q",
         x_mm=152.4, y_mm=101.6, unit=2, fields={"MPN": "X1"})
    tree = sch_io.parse_file(blank_project["sch"])
    assert {ed.get_symbol_property(n, "MPN") for n in ed.find_symbols_by_reference(tree, "U1")} == {"X1"}


# ===== D3: grid warnings ===================================================== #


def test_off_grid_symbol_origin_is_reported(mcp):
    res = call(mcp, "add_symbol", lib_id="MiniLib:Resistor", reference="R1", value="1k",
               x_mm=90.0, y_mm=101.6)
    assert res["warnings"] and "1.27" in res["warnings"][0]


def test_off_grid_wire_and_label_are_reported(mcp):
    assert call(mcp, "add_wire", x1_mm=10, y1_mm=10, x2_mm=20, y2_mm=10)["warnings"]
    assert call(mcp, "add_label", net_name="N", x_mm=10.1, y_mm=10.16)["warnings"]
    assert call(mcp, "add_wire", x1_mm=12.7, y1_mm=12.7, x2_mm=25.4, y2_mm=12.7)["warnings"] == []


# ===== batch items =========================================================== #


def test_add_items_applies_everything_in_one_write(mcp, blank_project):
    res = call(mcp, "add_items", items=[
        {"op": "wire", "x1": 25.4, "y1": 25.4, "x2": 50.8, "y2": 25.4},
        {"op": "junction", "x": 38.1, "y": 25.4},
        {"op": "label", "net": "N1", "x": 50.8, "y": 25.4},
        {"op": "global_label", "net": "G1", "x": 63.5, "y": 25.4, "shape": "input"},
        {"op": "text", "text": "note\nline2", "x": 25.4, "y": 38.1},
        {"op": "no_connect", "x": 76.2, "y": 25.4},
    ])
    assert res["applied"] == {"wire": 1, "junction": 1, "label": 1, "global_label": 1,
                              "text": 1, "no_connect": 1}
    assert res["warnings"] == []
    root = sch_io.parse_file(blank_project["sch"])
    for head in ("wire", "junction", "label", "global_label", "text", "no_connect"):
        assert len(sch_io.find_children(root, head)) == 1


def test_add_items_is_atomic(mcp, blank_project):
    before = blank_project["sch"].read_text()
    with pytest.raises(ValueError, match="op must be one of"):
        call(mcp, "add_items", items=[
            {"op": "wire", "x1": 25.4, "y1": 25.4, "x2": 50.8, "y2": 25.4},
            {"op": "teleport"},
        ])
    with pytest.raises(ValueError, match="needs"):
        call(mcp, "add_items", items=[{"op": "wire", "x1": 1}])
    assert blank_project["sch"].read_text() == before


def test_add_items_rejects_oversized_batch(mcp):
    with pytest.raises(ValueError, match="at most"):
        call(mcp, "add_items", items=[{"op": "junction", "x": 0, "y": 0}] * 5001)


# ===== custom power rails ==================================================== #


def test_custom_rail_borrows_stock_graphics(mcp, blank_project):
    res = call(mcp, "add_power_symbol", net="VCC_1V8_PL", x_mm=101.6, y_mm=101.6)
    assert res["lib_id"] == "power:+3V3" and res["reference"] == "#PWR0001"
    node = ed.find_symbol_by_reference(sch_io.parse_file(blank_project["sch"]), "#PWR0001")
    assert ed.get_symbol_property(node, "Value") == "VCC_1V8_PL"


def test_custom_ground_style_and_stock_net_unchanged(mcp):
    assert call(mcp, "add_power_symbol", net="AGND_ADC", style="GND",
                x_mm=101.6, y_mm=101.6)["lib_id"] == "power:GND"
    assert call(mcp, "add_power_symbol", net="GND", x_mm=127, y_mm=101.6)["lib_id"] == "power:GND"


def test_unknown_style_lists_available(mcp):
    with pytest.raises(KeyError, match="available styles"):
        call(mcp, "add_power_symbol", net="X", style="NOPE", x_mm=101.6, y_mm=101.6)


@needs_kicad
def test_custom_rail_names_the_net_in_kicad(mcp, blank_project):
    call(mcp, "add_symbol", lib_id="MiniLib:Resistor", reference="R1", value="1k",
         x_mm=101.6, y_mm=101.6)
    top = [p for p in call(mcp, "list_pins", reference="R1") if p["number"] == "1"][0]
    call(mcp, "add_power_symbol", net="VCC_1V8_PL", x_mm=top["position_mm"][0],
         y_mm=top["position_mm"][1])
    assert netlist_pins(blank_project["sch"])["VCC_1V8_PL"] == {("R1", "1")}
