"""Phase 16 — populate the PCB from the schematic in one pass."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from mcp.server.fastmcp import FastMCP

from kicad_claude.adapters import pcb_editor as ped
from kicad_claude.adapters import pcb_populate as pop
from kicad_claude.adapters import sch_io
from kicad_claude.tools import project as project_tools
from kicad_claude.tools import sync as sync_tools

from .carrier_support import FIXTURES, call, needs_kicad

NETLIST = """<export version="E"><components>
<comp ref="R10"><value>1k</value><footprint>Lib:FP</footprint></comp>
<comp ref="R2"><value>2k</value><footprint>Lib:FP</footprint></comp>
<comp ref="U_X1"><value>chip</value></comp>
<comp ref="C1"><value>1u</value><footprint> Lib:Cap </footprint></comp>
</components></export>"""


def test_netlist_parts_are_naturally_sorted_and_trimmed(tmp_path: Path):
    xml = tmp_path / "n.xml"
    xml.write_text(NETLIST)
    parts = pop.netlist_parts(xml)
    assert [p.reference for p in parts] == ["C1", "R2", "R10", "U_X1"]
    assert parts[0].footprint == "Lib:Cap" and parts[3].footprint == ""


def test_plan_skips_present_and_footprintless_parts(tmp_path: Path):
    xml = tmp_path / "n.xml"
    xml.write_text(NETLIST)
    plan = pop.plan_placements(pop.netlist_parts(xml), {"R2"}, columns=2, pitch_mm=10, origin=(0, 100))
    assert [p.reference for p, _, _ in plan["place"]] == ["C1", "R10"]
    assert plan["no_footprint"] == ["U_X1"] and plan["already_on_board"] == ["R2"]


def test_grid_is_row_major_going_down():
    assert pop.grid_position(0, 3, 10, (5, 100)) == (5, 100)
    assert pop.grid_position(4, 3, 10, (5, 100)) == (15, 90)


def test_bad_grid_arguments_rejected():
    with pytest.raises(ValueError):
        pop.plan_placements([], set(), columns=0)


def test_legacy_fp_text_becomes_reference_property():
    fp = ped.fetch_footprint_def(FIXTURES / "OldFP.pretty" / "OldPad.kicad_mod")
    placed = ped._build_placed_footprint(fp, "OldFP:OldPad", "R7", "47k", 10, 10, 0, "F.Cu")
    props = {p[1]: p[2] for p in sch_io.find_children(placed, "property")}
    assert props == {"Reference": "R7", "Value": "47k"}
    assert not [c for c in sch_io.find_children(placed, "fp_text") if c[1] in ("reference", "value")]
    assert sch_io.find_children(placed, "fp_text")[0][1] == sch_io.sym("user")  # other text kept


@pytest.fixture
def populate_env(mcp, blank_project, monkeypatch):
    """Project with a fixture footprint library and a tool server incl. sync/project."""
    proj = blank_project["sch"].parent
    pretty = proj / "lib" / "OldFP.pretty"
    shutil.copytree(FIXTURES / "OldFP.pretty", pretty)
    from kicad_claude.adapters import vendor_import

    vendor_import.update_fp_lib_table(proj, "OldFP")
    sync_tools.register(mcp)
    project_tools.register(mcp)
    return mcp, proj


def build_schematic(mcp):
    for ref, x, fp in (("R1", 76.2, "OldFP:OldPad"), ("R2", 101.6, "OldFP:OldPad"), ("R3", 127, "")):
        call(mcp, "add_symbol", lib_id="MiniLib:Resistor", reference=ref, value="1k",
             x_mm=x, y_mm=101.6, footprint=fp)
        call(mcp, "connect_pins", reference=ref,
             connections=[{"pin": "1", "net": f"N_{ref}"}, {"pin": "2", "net": "COMMON", "scope": "global"}])


@needs_kicad
def test_place_footprints_from_schematic_end_to_end(populate_env):
    mcp, proj = populate_env
    build_schematic(mcp)
    res = call(mcp, "place_footprints_from_schematic", columns=2, pitch_mm=10)
    assert res["placed"] == 2 and res["no_footprint"] == ["R3"] and res["unresolved"] == []
    pcb = sch_io.parse_file(proj / "demo.kicad_pcb")
    assert sorted(ped.all_footprint_references(pcb)) == ["R1", "R2"]  # not REF**
    again = call(mcp, "place_footprints_from_schematic")
    assert again["placed"] == 0 and again["already_on_board"] == 2


@needs_kicad
def test_pads_get_their_nets_after_populate(populate_env):
    mcp, proj = populate_env
    build_schematic(mcp)
    call(mcp, "place_footprints_from_schematic")
    res = call(mcp, "update_pcb_from_schematic")
    assert res["matched_footprints"] == 2 and res["pad_assignments_made"] == 4
    text = (proj / "demo.kicad_pcb").read_text()
    assert '"/N_R1"' in text and '"COMMON"' in text


def test_unresolvable_footprint_is_reported_not_fatal(populate_env, monkeypatch):
    mcp, proj = populate_env
    call(mcp, "add_symbol", lib_id="MiniLib:Resistor", reference="R1", value="1k",
         x_mm=76.2, y_mm=101.6, footprint="Nope:Missing")
    call(mcp, "add_symbol", lib_id="MiniLib:Resistor", reference="R2", value="1k",
         x_mm=101.6, y_mm=101.6, footprint="OldFP:OldPad")
    res = call(mcp, "place_footprints_from_schematic")
    assert res["placed"] == 1
    assert res["unresolved"][0]["reference"] == "R1" and "Nope:Missing" in res["unresolved"][0]["footprint"]


@needs_kicad
def test_project_summary_survives_legacy_footprints(populate_env):
    mcp, proj = populate_env
    build_schematic(mcp)
    call(mcp, "place_footprints_from_schematic")
    state_after = call(mcp, "get_project_state")
    assert state_after["footprints"] == 2 and state_after["symbols"] == 3
