"""Phase 16 — bulk pin wiring (connect_pins) and global labels."""

from __future__ import annotations

import pytest

from kicad_claude.adapters import sch_connect, sch_io

from .carrier_support import call, needs_kicad, netlist_pins


def place_quad(mcp, unit=1, x=101.6, rotation=0):
    return call(mcp, "add_symbol", lib_id="UnitLib:Quad", reference="U1", value="Quad",
                x_mm=x, y_mm=101.6, unit=unit, rotation=rotation)


def count(path, head: str) -> int:
    return len(sch_io.find_children(sch_io.parse_file(path), head))


def test_connect_pins_writes_stub_and_label_per_pin(mcp, blank_project):
    place_quad(mcp)
    res = call(mcp, "connect_pins", reference="U1", unit=1, connections=[
        {"pin": "1", "net": "A_IN"}, {"pin": "2", "net": "Y_OUT"},
    ])
    assert res["labelled"] == 2 and res["no_connect"] == 0
    assert count(blank_project["sch"], "wire") == 2
    assert count(blank_project["sch"], "label") == 2


def test_nc_connections_place_no_connect_markers(mcp, blank_project):
    place_quad(mcp)
    res = call(mcp, "connect_pins", reference="U1", unit=1,
               connections=[{"pin": "1", "nc": True}, {"pin": "2", "net": "Y"}])
    assert res["no_connect"] == 1
    assert count(blank_project["sch"], "no_connect") == 1


def test_unknown_pin_writes_nothing(mcp, blank_project):
    place_quad(mcp)
    before = blank_project["sch"].read_text()
    with pytest.raises(KeyError, match="'99'"):
        call(mcp, "connect_pins", reference="U1", connections=[
            {"pin": "1", "net": "OK"}, {"pin": "99", "net": "BAD"},
        ])
    assert blank_project["sch"].read_text() == before


def test_pin_on_unplaced_unit_is_rejected(mcp):
    place_quad(mcp, unit=1)
    with pytest.raises(KeyError, match="not on a placed unit"):
        call(mcp, "connect_pins", reference="U1", connections=[{"pin": "3", "net": "X"}])


def test_duplicate_pin_and_missing_net_rejected(mcp):
    place_quad(mcp)
    with pytest.raises(ValueError, match="listed twice"):
        call(mcp, "connect_pins", reference="U1",
             connections=[{"pin": "1", "net": "A"}, {"pin": "1", "net": "B"}])
    with pytest.raises(ValueError, match="give `net` or `nc"):
        call(mcp, "connect_pins", reference="U1", connections=[{"pin": "1"}])


def test_bad_scope_and_stub_rejected(mcp):
    place_quad(mcp)
    with pytest.raises(ValueError, match="scope must be"):
        call(mcp, "connect_pins", reference="U1", scope="galactic",
             connections=[{"pin": "1", "net": "A"}])
    with pytest.raises(ValueError, match="stub_mm"):
        call(mcp, "connect_pins", reference="U1", stub_mm=3.0,
             connections=[{"pin": "1", "net": "A"}])


def test_hierarchical_scope_refused_on_root(mcp):
    place_quad(mcp)
    with pytest.raises(RuntimeError, match="sub-sheet"):
        call(mcp, "connect_pins", reference="U1", scope="hierarchical",
             connections=[{"pin": "1", "net": "A"}])


def test_global_label_node_shape(mcp, blank_project):
    call(mcp, "add_global_label", net_name="VBUS", x_mm=50.8, y_mm=50.8, shape="input")
    node = sch_io.find_child(sch_io.parse_file(blank_project["sch"]), "global_label")
    assert node[1] == "VBUS"
    assert sch_io.find_child(node, "shape")[1] == sch_io.sym("input")
    assert sch_io.find_child(node, "property")[1] == "Intersheetrefs"


def test_global_label_rejects_bad_shape():
    with pytest.raises(ValueError, match="shape must be"):
        sch_connect.add_global_label([sch_io.sym("kicad_sch")], "X", 0, 0, shape="sideways")


@needs_kicad
@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_stubs_leave_every_pin_outward_and_connect_in_kicad(mcp, blank_project, rotation):
    place_quad(mcp, unit=1, x=76.2, rotation=rotation)
    place_quad(mcp, unit=2, x=152.4, rotation=rotation)
    call(mcp, "connect_pins", reference="U1", connections=[
        {"pin": "1", "net": "N1"}, {"pin": "2", "net": "N2"},
        {"pin": "3", "net": "N3"}, {"pin": "4", "net": "N4"},
    ])
    nets = netlist_pins(blank_project["sch"])
    for n in "1234":
        assert nets[f"/N{n}"] == {("U1", n)}


@needs_kicad
def test_global_label_joins_nets_across_sheets_in_kicad(mcp, blank_project):
    call(mcp, "add_sheet", sheet_name="a", sheet_filename="a.kicad_sch")
    place_quad(mcp, unit=1)
    call(mcp, "connect_pins", reference="U1", unit=1, scope="global",
         connections=[{"pin": "1", "net": "SHARED"}])
    call(mcp, "set_active_sheet", sheet="a")
    call(mcp, "add_symbol", lib_id="MiniLib:Resistor", reference="R1", value="1k",
         x_mm=101.6, y_mm=101.6)
    call(mcp, "connect_pins", reference="R1", scope="global",
         connections=[{"pin": "1", "net": "SHARED"}])
    nets = netlist_pins(blank_project["sch"])
    assert nets["SHARED"] == {("U1", "1"), ("R1", "1")}


@needs_kicad
def test_local_label_does_not_join_across_sheets_in_kicad(mcp, blank_project):
    call(mcp, "add_sheet", sheet_name="a", sheet_filename="a.kicad_sch")
    place_quad(mcp, unit=1)
    call(mcp, "connect_pins", reference="U1", unit=1, connections=[{"pin": "1", "net": "SHARED"}])
    call(mcp, "set_active_sheet", sheet="a")
    call(mcp, "add_symbol", lib_id="MiniLib:Resistor", reference="R1", value="1k",
         x_mm=101.6, y_mm=101.6)
    call(mcp, "connect_pins", reference="R1", connections=[{"pin": "1", "net": "SHARED"}])
    nets = netlist_pins(blank_project["sch"])
    assert nets["/SHARED"] == {("U1", "1")} and nets["/a/SHARED"] == {("R1", "1")}


@needs_kicad
def test_480_pins_wired_in_one_call_land_on_their_nets(mcp, blank_project):
    pins = [{"number": f"{u}_{i}", "name": f"S{i}", "unit": u}
            for u in range(1, 5) for i in range(1, 121)]
    call(mcp, "create_symbol", lib_name="som", symbol_name="K26", pins=pins,
         reference_prefix="J")
    call(mcp, "set_sheet_paper", paper="A2")
    for unit in range(1, 5):
        call(mcp, "add_symbol", lib_id="som:K26", reference="J1", value="K26",
             x_mm=101.6 + (unit - 1) * 101.6, y_mm=203.2, unit=unit)
        call(mcp, "connect_pins", reference="J1", unit=unit, scope="global", connections=[
            {"pin": f"{unit}_{i}", "net": f"NET_{unit}_{i}"} for i in range(1, 121)
        ])
    nets = netlist_pins(blank_project["sch"])
    wired = {n: p for n, p in nets.items() if n.startswith("NET_")}
    assert len(wired) == 480
    assert all(len(p) == 1 for p in wired.values())
    assert wired["NET_3_77"] == {("J1", "3_77")}
