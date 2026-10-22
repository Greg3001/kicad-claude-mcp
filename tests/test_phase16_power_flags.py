"""Phase 16 — PWR_FLAG helper and multi-unit-aware annotation."""

from __future__ import annotations

import pytest

from kicad_claude.adapters import power_flags
from kicad_claude.tools import power_flags as flag_tools

from .carrier_support import call, erc_counts, needs_kicad


def test_undriven_refs_reads_type_and_reference_not_the_words():
    data = {"sheets": [
        {"path": "/", "violations": [
            {"type": "power_pin_not_driven",
             "items": [{"description": "Símbolo #PWR0007 Pin 1 [Entrada de alimentación]"}]},
            {"type": "pin_not_connected", "items": [{"description": "Symbol J1 Pin 3"}]},
        ]},
        {"path": "/som/", "violations": [
            {"type": "power_pin_not_driven", "items": [{"description": "Symbol #PWR0002 Pin 1"}]}]},
    ]}
    assert power_flags.undriven_refs(data) == [("/", "#PWR0007"), ("/som/", "#PWR0002")]


def test_sheet_path_to_name():
    assert power_flags.sheet_name_of("/") is None
    assert power_flags.sheet_name_of("/som/") == "som"
    with pytest.raises(ValueError, match="nested"):
        power_flags.sheet_name_of("/a/b/")


def rail_between(mcp, ref="R1", x=101.6):
    """A resistor from +3V3 to GND, power symbols sitting on its pins."""
    call(mcp, "add_symbol", lib_id="MiniLib:Resistor", reference=ref, value="1k", x_mm=x, y_mm=101.6)
    for pin, net in (("1", "+3V3"), ("2", "GND")):
        px, py = [p["position_mm"] for p in call(mcp, "list_pins", reference=ref)
                  if p["number"] == pin][0]
        call(mcp, "add_power_symbol", net=net, x_mm=px, y_mm=py)


@pytest.fixture
def flags_mcp(mcp):
    flag_tools.register(mcp)
    return mcp


@needs_kicad
def test_one_flag_per_undriven_net_clears_erc(flags_mcp, blank_project):
    rail_between(flags_mcp, "R1", 101.6)
    rail_between(flags_mcp, "R2", 152.4)  # same two nets again
    assert erc_counts(blank_project["sch"])["power_pin_not_driven"] == 2  # ERC reports per net
    res = call(flags_mcp, "add_power_flags")
    assert res["flags_placed"] == 2
    assert {f["net"] for f in res["flags"]} == {"+3V3", "GND"}
    assert res["undriven_before"] == 2 and res["undriven_after"] == 0
    assert all(f["reference"].startswith("#FLG") for f in res["flags"])


@needs_kicad
def test_flag_goes_on_the_sheet_that_owns_the_symbol(flags_mcp, blank_project):
    call(flags_mcp, "add_sheet", sheet_name="som", sheet_filename="som.kicad_sch")
    call(flags_mcp, "set_active_sheet", sheet="som")
    rail_between(flags_mcp)
    call(flags_mcp, "set_active_sheet", sheet="root")
    res = call(flags_mcp, "add_power_flags")
    assert {f["sheet"] for f in res["flags"]} == {"som.kicad_sch"}
    assert res["undriven_after"] == 0
    assert call(flags_mcp, "get_active_sheet")["active_sheet"] == "root"  # restored


@needs_kicad
def test_nothing_to_do_places_nothing(flags_mcp):
    res = call(flags_mcp, "add_power_flags")
    assert res["flags_placed"] == 0 and res["undriven_before"] == 0


# ===== multi-unit annotation ================================================= #


def place_unit(mcp, unit, x, ref="U?"):
    call(mcp, "add_symbol", lib_id="UnitLib:Quad", reference=ref, value="Q",
         x_mm=x, y_mm=101.6, unit=unit)


def refs_by_unit(blank_project):
    from kicad_claude.adapters import sch_editor as ed
    from kicad_claude.adapters import sch_io

    tree = sch_io.parse_file(blank_project["sch"])
    return sorted(
        (ed.get_symbol_property(n, "Reference"), ed.symbol_unit(n))
        for n in ed.iter_instance_symbols(tree)
    )


@pytest.fixture
def sync_mcp(mcp):
    from kicad_claude.tools import sync as sync_tools

    sync_tools.register(mcp)
    return mcp


def test_annotation_keeps_units_of_one_part_together(sync_mcp, blank_project):
    for unit, x in ((1, 50.8), (2, 76.2), (1, 101.6), (2, 127.0)):
        place_unit(sync_mcp, unit, x)
    call(sync_mcp, "annotate_schematic")
    assert refs_by_unit(blank_project) == [("U1", 1), ("U1", 2), ("U2", 1), ("U2", 2)]


def test_annotation_of_single_unit_parts_is_unchanged(sync_mcp, blank_project):
    for x in (50.8, 76.2, 101.6):
        call(sync_mcp, "add_symbol", lib_id="MiniLib:Resistor", reference="R?", value="1k",
             x_mm=x, y_mm=101.6)
    call(sync_mcp, "annotate_schematic")
    assert [r for r, _ in refs_by_unit(blank_project)] == ["R1", "R2", "R3"]


def test_annotation_continues_after_existing_numbers(sync_mcp, blank_project):
    place_unit(sync_mcp, 1, 50.8, ref="U5")
    place_unit(sync_mcp, 2, 76.2, ref="U5")
    place_unit(sync_mcp, 1, 101.6)
    place_unit(sync_mcp, 2, 127.0)
    call(sync_mcp, "annotate_schematic")
    assert refs_by_unit(blank_project) == [("U5", 1), ("U5", 2), ("U6", 1), ("U6", 2)]


def test_a_part_whose_units_are_all_placed_does_not_swallow_the_next_part(sync_mcp, blank_project):
    # two unit-1 nodes in a row cannot both belong to one two-unit part
    place_unit(sync_mcp, 1, 50.8)
    place_unit(sync_mcp, 1, 76.2)
    call(sync_mcp, "annotate_schematic")
    assert refs_by_unit(blank_project) == [("U1", 1), ("U2", 1)]


@needs_kicad
def test_annotated_multi_unit_part_is_one_component_in_kicad(sync_mcp, blank_project):
    from .carrier_support import netlist_pins

    place_unit(sync_mcp, 1, 76.2)
    place_unit(sync_mcp, 2, 152.4)
    call(sync_mcp, "annotate_schematic")
    call(sync_mcp, "connect_pins", reference="U1", connections=[
        {"pin": "1", "net": "A"}, {"pin": "3", "net": "B"}])
    nets = netlist_pins(blank_project["sch"])
    assert nets["/A"] == {("U1", "1")} and nets["/B"] == {("U1", "3")}
    assert erc_counts(blank_project["sch"])["missing_unit"] == 0
