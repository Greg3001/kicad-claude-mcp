"""Phase 16 — sheet pins derived from a child's hierarchical labels."""

from __future__ import annotations

import pytest

from kicad_claude.adapters import sch_editor as ed
from kicad_claude.adapters import sch_io, sch_sheets

from .carrier_support import call, erc_counts, needs_kicad, netlist_pins


def build_two_sheets(mcp):
    """Sheets `a` and `b`, each with a resistor whose pins are exposed upward."""
    for i, name in enumerate(("a", "b")):
        call(mcp, "add_sheet", sheet_name=name, sheet_filename=f"{name}.kicad_sch",
             x_mm=25.4 + 76.2 * i, y_mm=127, width_mm=38.1, height_mm=12.7)
        call(mcp, "set_active_sheet", sheet=name)
        call(mcp, "add_symbol", lib_id="MiniLib:Resistor", reference=f"R{i + 1}",
             value="1k", x_mm=101.6, y_mm=101.6)
        call(mcp, "connect_pins", reference=f"R{i + 1}", scope="hierarchical", connections=[
            {"pin": "1", "net": "SIG", "shape": "input"},
            {"pin": "2", "net": f"OUT_{name}", "shape": "output"},
        ])
    call(mcp, "set_active_sheet", sheet="root")


def test_child_interface_reads_hierarchical_labels():
    tree = sch_io.parse_file_text(
        '(kicad_sch (hierarchical_label "A" (shape input) (at 0 0 0))'
        ' (hierarchical_label "B" (shape output) (at 0 0 0))'
        ' (hierarchical_label "A" (shape input) (at 5 0 0)))'
    )
    assert sch_sheets.child_interface(tree) == [("A", "input"), ("B", "output")]


def test_sync_adds_one_pin_per_label_on_the_right_edges(mcp, blank_project):
    build_two_sheets(mcp)
    res = call(mcp, "sync_sheet_pins", sheet_name="a")
    assert res["added"] == ["SIG", "OUT_a"] and res["stale"] == []
    by_name = {p["name"]: p for p in res["pins"]}
    assert by_name["SIG"]["angle"] == 180 and by_name["OUT_a"]["angle"] == 0
    assert by_name["OUT_a"]["position_mm"][0] == pytest.approx(25.4 + 38.1)
    assert by_name["SIG"]["position_mm"][0] == pytest.approx(25.4)


def test_sync_is_idempotent_and_reports_stale(mcp, blank_project):
    build_two_sheets(mcp)
    call(mcp, "sync_sheet_pins", sheet_name="a")
    again = call(mcp, "sync_sheet_pins", sheet_name="a")
    assert again["added"] == []
    call(mcp, "set_active_sheet", sheet="a")
    tree = sch_io.parse_file(blank_project["sch"].parent / "a.kicad_sch")
    tree[:] = [c for c in tree if not (sch_io.is_call(c, "hierarchical_label") and c[1] == "SIG")]
    sch_io.write_file(blank_project["sch"].parent / "a.kicad_sch", tree)
    call(mcp, "set_active_sheet", sheet="root")
    assert call(mcp, "sync_sheet_pins", sheet_name="a")["stale"] == ["SIG"]


def test_sheet_grows_to_fit_many_pins(mcp, blank_project):
    call(mcp, "add_sheet", sheet_name="big", sheet_filename="big.kicad_sch",
         x_mm=25.4, y_mm=127, width_mm=38.1, height_mm=5.08)
    call(mcp, "set_active_sheet", sheet="big")
    call(mcp, "add_symbol", lib_id="UnitLib:Quad", reference="U1", value="Q", x_mm=101.6, y_mm=101.6)
    call(mcp, "connect_pins", reference="U1", unit=1, scope="hierarchical", connections=[
        {"pin": "1", "net": "I1", "shape": "input"}, {"pin": "2", "net": "O1", "shape": "output"}])
    call(mcp, "set_active_sheet", sheet="root")
    res = call(mcp, "sync_sheet_pins", sheet_name="big")
    assert res["height_mm"] >= 2.54 * 2
    root = sch_io.parse_file(blank_project["sch"])
    assert len(sch_io.find_children(ed.find_sheet_by_name(root, "big"), "pin")) == 2


def test_unknown_sheet_rejected(mcp):
    with pytest.raises(KeyError, match="no sheet named"):
        call(mcp, "sync_sheet_pins", sheet_name="nope")
    with pytest.raises(KeyError, match="no sheet named"):
        call(mcp, "label_sheet_pins", sheet_name="nope")


def test_label_sheet_pins_needs_pins_and_valid_scope(mcp):
    build_two_sheets(mcp)
    with pytest.raises(ValueError, match="no pins"):
        call(mcp, "label_sheet_pins", sheet_name="a")
    with pytest.raises(ValueError, match="scope must be"):
        call(mcp, "label_sheet_pins", sheet_name="a", scope="hierarchical")


@needs_kicad
def test_two_sheets_joined_by_global_pin_labels_in_kicad(mcp, blank_project):
    build_two_sheets(mcp)
    for name in ("a", "b"):
        call(mcp, "sync_sheet_pins", sheet_name=name)
        call(mcp, "label_sheet_pins", sheet_name=name, scope="global")
    nets = netlist_pins(blank_project["sch"])
    # SIG enters both sheets under one global name: R1.1 and R2.1 are one net
    assert nets["SIG"] == {("R1", "1"), ("R2", "1")}
    assert nets["OUT_a"] == {("R1", "2")}
    counts = erc_counts(blank_project["sch"])
    assert counts["pin_not_connected"] == 0  # every sheet pin is wired
