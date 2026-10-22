"""Phase 16 — fixes found while preparing a large SOM-carrier schematic.

See docs/AUDIT_2026-10.md for the defect list. Pure-Python tests run
everywhere; the kicad-cli round trips are skipped when KiCad is absent.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from kicad_claude.adapters import sch_editor as ed
from kicad_claude.adapters import sch_io, symbol_lib

from .carrier_support import (
    FIXTURES,
    KICAD_CLI,
    call,
    erc_counts,
    needs_kicad,
    netlist_pins,
    refs_on,
)

UNIT_LIB = FIXTURES / "UnitLib.kicad_sym"
MINI_LIB = FIXTURES / "MiniLib.kicad_sym"


# ===== extends resolution ================================================== #


def _pin_numbers(node: list) -> list[str]:
    return sorted(ed.collect_pin_numbers(node))


def test_extends_stub_is_flattened_with_base_pins():
    node = ed.fetch_symbol_def(MINI_LIB, "R_Small")
    assert sch_io.find_child(node, "extends") is None
    assert _pin_numbers(node) == ["1", "2"]
    assert ed.get_symbol_property(node, "Value") == "R_Small"
    assert ed.get_symbol_property(node, "Description") == "Small resistor symbol"


def test_flattened_units_are_renamed_to_derived_name():
    node = ed.fetch_symbol_def(MINI_LIB, "R_Small")
    sub_names = [c[1] for c in node[2:] if sch_io.is_call(c, "symbol")]
    assert sub_names == ["R_Small_0_1"]


def test_extends_chain_resolves_through_two_levels():
    node = ed.fetch_symbol_def(UNIT_LIB, "Quad_C")
    assert node[1] == "Quad_C"
    assert _pin_numbers(node) == ["1", "2", "3", "4", "8"]
    assert ed.get_symbol_property(node, "Value") == "Quad_C"
    # inherited from Quad_B, which overrode Quad's description
    assert ed.get_symbol_property(node, "Description") == "Variant of Quad"


def test_extends_cycle_is_rejected(tmp_path: Path):
    lib = tmp_path / "Loop.kicad_sym"
    lib.write_text(
        '(kicad_symbol_lib (version 20251024)'
        ' (symbol "A" (extends "B") (property "Value" "A" (at 0 0 0)))'
        ' (symbol "B" (extends "A") (property "Value" "B" (at 0 0 0))))'
    )
    with pytest.raises(ValueError, match="too deep"):
        symbol_lib.load_symbol_def(lib, "A")


def test_unit_numbers_ignore_shared_unit_zero():
    node = ed.fetch_symbol_def(UNIT_LIB, "Quad")
    assert symbol_lib.unit_numbers(node) == [1, 2]


def test_pins_of_unit_adds_shared_pins_only_for_requested_unit():
    node = ed.fetch_symbol_def(UNIT_LIB, "Quad")
    nums = lambda u: sorted(  # noqa: E731
        ed._pin_id(p)[0] for p, _ in symbol_lib.pins_of_unit(node, u)
    )
    assert nums(1) == ["1", "2", "8"]
    assert nums(2) == ["3", "4", "8"]


def place(sch_path: Path, lib: Path, name: str, ref: str, x: float, y: float, **kw) -> list:
    tree = sch_io.parse_file(sch_path)
    node = ed.add_symbol(
        tree, qualified_lib_id=f"T:{name}", reference=ref, value=name,
        x_mm=x, y_mm=y, rotation=0, sym_def_node=ed.fetch_symbol_def(lib, name),
        project_name="demo", **kw,
    )
    sch_io.write_file(sch_path, tree)
    return node


@needs_kicad
def test_extends_symbol_has_pins_and_no_lib_mismatch_in_kicad(blank_project):
    sch = blank_project["sch"]
    place(sch, MINI_LIB, "R_Small", "R1", 101.6, 101.6)
    counts = erc_counts(sch)
    assert counts["pin_not_connected"] == 2  # both pins exist
    assert counts["lib_symbol_mismatch"] == 0


# ===== project-wide power references ======================================= #


def test_power_refs_unique_across_sheets(mcp, blank_project):
    call(mcp, "add_sheet", sheet_name="a", sheet_filename="a.kicad_sch")
    call(mcp, "add_sheet", sheet_name="b", sheet_filename="b.kicad_sch")
    proj = blank_project["sch"].parent
    call(mcp, "add_power_symbol", net="GND", x_mm=50.8, y_mm=50.8)
    for sheet in ("a", "b"):
        call(mcp, "set_active_sheet", sheet=sheet)
        call(mcp, "add_power_symbol", net="GND", x_mm=50.8, y_mm=50.8)
    refs = []
    for f in ("demo.kicad_sch", "a.kicad_sch", "b.kicad_sch"):
        refs += refs_on(proj / f)
    assert sorted(refs) == ["#PWR0001", "#PWR0002", "#PWR0003"]


def test_power_flag_gets_flg_reference(mcp):
    call(mcp, "add_power_symbol", net="GND", x_mm=50.8, y_mm=50.8)
    res = call(mcp, "add_power_symbol", net="PWR_FLAG", x_mm=50.8, y_mm=50.8)
    assert res["reference"] == "#FLG0001"


@needs_kicad
def test_multi_sheet_power_symbols_pass_annotation_in_kicad(mcp, blank_project):
    call(mcp, "add_sheet", sheet_name="a", sheet_filename="a.kicad_sch")
    call(mcp, "add_power_symbol", net="GND", x_mm=50.8, y_mm=50.8)
    call(mcp, "set_active_sheet", sheet="a")
    call(mcp, "add_power_symbol", net="GND", x_mm=50.8, y_mm=50.8)
    out = blank_project["sch"].with_suffix(".net")
    run = subprocess.run(
        [str(KICAD_CLI), "sch", "export", "netlist", "-o", str(out),
         str(blank_project["sch"])],
        capture_output=True, text=True, timeout=120, check=False,
    )
    noise = [
        ln for ln in (run.stdout + run.stderr).splitlines()
        if ln.strip() and "Fontconfig" not in ln
    ]
    assert run.returncode == 0
    assert noise == []  # any annotation warning would be printed here


# ===== multi-unit symbols ================================================== #


def place_quad(server, ref="U1", **kw):
    return call(server, "add_symbol", lib_id="UnitLib:Quad", reference=ref,
                value="Quad", **kw)


def test_place_second_unit_of_same_reference(mcp, blank_project):
    first = place_quad(mcp, x_mm=76.2, y_mm=101.6, unit=1)
    second = place_quad(mcp, x_mm=127, y_mm=101.6, unit=2)
    assert (first["unit"], second["unit"]) == (1, 2)
    assert first["units_in_symbol"] == [1, 2]
    tree = sch_io.parse_file(blank_project["sch"])
    nodes = ed.find_symbols_by_reference(tree, "U1")
    assert sorted(ed.symbol_unit(n) for n in nodes) == [1, 2]


def test_instance_lists_only_its_units_pins(mcp, blank_project):
    place_quad(mcp, x_mm=76.2, y_mm=101.6, unit=1)
    place_quad(mcp, x_mm=127, y_mm=101.6, unit=2)
    tree = sch_io.parse_file(blank_project["sch"])
    by_unit = {
        ed.symbol_unit(n): sorted(p[1] for p in sch_io.find_children(n, "pin"))
        for n in ed.find_symbols_by_reference(tree, "U1")
    }
    assert by_unit == {1: ["1", "2", "8"], 2: ["3", "4", "8"]}


def test_same_unit_twice_is_refused(mcp):
    place_quad(mcp, x_mm=76.2, y_mm=101.6, unit=1)
    with pytest.raises(ValueError, match="unit 1 already exists"):
        place_quad(mcp, x_mm=127, y_mm=101.6, unit=1)


def test_unknown_unit_is_refused(mcp):
    with pytest.raises(ValueError, match="unit 3 not in symbol"):
        place_quad(mcp, x_mm=76.2, y_mm=101.6, unit=3)


def test_other_lib_id_cannot_reuse_reference(mcp):
    place_quad(mcp, x_mm=76.2, y_mm=101.6, unit=1)
    with pytest.raises(ValueError, match="already exists"):
        call(mcp, "add_symbol", lib_id="MiniLib:Resistor", reference="U1",
             value="10k", x_mm=127, y_mm=101.6)


def test_later_unit_inherits_fields_of_first(mcp, blank_project):
    place_quad(mcp, x_mm=76.2, y_mm=101.6, unit=1)
    call(mcp, "set_symbol_field", reference="U1", field="MPN", value="ABC-123")
    place_quad(mcp, x_mm=127, y_mm=101.6, unit=2)
    tree = sch_io.parse_file(blank_project["sch"])
    mpns = {ed.get_symbol_property(n, "MPN") for n in ed.find_symbols_by_reference(tree, "U1")}
    assert mpns == {"ABC-123"}


def test_set_field_updates_every_unit(mcp, blank_project):
    place_quad(mcp, x_mm=76.2, y_mm=101.6, unit=1)
    place_quad(mcp, x_mm=127, y_mm=101.6, unit=2)
    call(mcp, "set_symbol_field", reference="U1", field="Footprint", value="Pkg:SOIC-8")
    tree = sch_io.parse_file(blank_project["sch"])
    fps = {ed.get_symbol_property(n, "Footprint") for n in ed.find_symbols_by_reference(tree, "U1")}
    assert fps == {"Pkg:SOIC-8"}


def test_list_pins_reports_unit_and_filters(mcp):
    place_quad(mcp, x_mm=76.2, y_mm=101.6, unit=1)
    place_quad(mcp, x_mm=127, y_mm=101.6, unit=2)
    both = call(mcp, "list_pins", reference="U1")
    assert {p["unit"] for p in both} == {1, 2}
    only2 = call(mcp, "list_pins", reference="U1", unit=2)
    assert sorted(p["number"] for p in only2) == ["3", "4", "8"]
    # pin 3 sits on unit 2's own body, 50.8 mm to the right of unit 1
    p3 = call(mcp, "get_pin_position", reference="U1", pin="3")
    assert p3["position_mm"][0] == pytest.approx(127 - 7.62)


def test_move_requires_unit_when_several_are_placed(mcp):
    place_quad(mcp, x_mm=76.2, y_mm=101.6, unit=1)
    place_quad(mcp, x_mm=127, y_mm=101.6, unit=2)
    with pytest.raises(ValueError, match="pass unit="):
        call(mcp, "move_symbol", reference="U1", x_mm=50.8, y_mm=50.8)
    call(mcp, "move_symbol", reference="U1", x_mm=50.8, y_mm=50.8, unit=2)
    pins = call(mcp, "list_pins", reference="U1", unit=2)
    assert min(p["position_mm"][0] for p in pins) == pytest.approx(50.8 - 7.62)


def test_remove_drops_all_units_or_one(mcp, blank_project):
    place_quad(mcp, x_mm=76.2, y_mm=101.6, unit=1)
    place_quad(mcp, x_mm=127, y_mm=101.6, unit=2)
    call(mcp, "remove_symbol", reference="U1", unit=2)
    tree = sch_io.parse_file(blank_project["sch"])
    assert [ed.symbol_unit(n) for n in ed.find_symbols_by_reference(tree, "U1")] == [1]
    call(mcp, "remove_symbol", reference="U1")
    tree = sch_io.parse_file(blank_project["sch"])
    assert ed.find_symbols_by_reference(tree, "U1") == []


@needs_kicad
@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_unit_pin_positions_connect_in_kicad(mcp, blank_project, rotation):
    place_quad(mcp, x_mm=76.2, y_mm=101.6, unit=1, rotation=rotation)
    place_quad(mcp, x_mm=152.4, y_mm=101.6, unit=2, rotation=rotation)
    for p in call(mcp, "list_pins", reference="U1"):
        if p["number"] != "8":
            call(mcp, "add_label", net_name=f"N{p['number']}",
                 x_mm=p["position_mm"][0], y_mm=p["position_mm"][1])
    nets = netlist_pins(blank_project["sch"])
    for n in ("1", "2", "3", "4"):
        assert nets[f"/N{n}"] == {("U1", n)}


@needs_kicad
def test_both_units_placed_leaves_no_missing_unit_warning(mcp, blank_project):
    place_quad(mcp, x_mm=76.2, y_mm=101.6, unit=1)
    assert erc_counts(blank_project["sch"])["missing_unit"] == 1
    place_quad(mcp, x_mm=152.4, y_mm=101.6, unit=2)
    assert erc_counts(blank_project["sch"])["missing_unit"] == 0


# ===== strings with line breaks ============================================ #


def test_newline_is_escaped_not_written_raw():
    out = sch_io.dumps([sch_io.sym("text"), "a\nb\r\nc"])
    assert "\n" not in out
    assert out == '(text "a\\nb\\r\\nc")'


def test_multiline_string_round_trips():
    node = [sch_io.sym("text"), 'line1\nline2 "q" \\ end']
    assert sch_io.parse_file_text(sch_io.dumps(node)) == node


# ===== batch 2: format version, library cache, reference format ============== #


def test_blank_sheets_carry_the_kicad10_format_version(blank_project, tmp_path):
    from kicad_claude.templates.blank import write_blank_schematic

    child = write_blank_schematic(tmp_path / "c.kicad_sch")
    for path in (blank_project["sch"], child):
        assert sch_io.find_child(sch_io.parse_file(path), "version")[1] >= 20260306


def test_library_is_parsed_once_and_reparsed_when_it_changes(tmp_path, monkeypatch):
    lib = tmp_path / "L.kicad_sym"
    lib.write_text(MINI_LIB.read_text())
    calls = []
    real = symbol_lib.sexpdata.loads
    monkeypatch.setattr(symbol_lib.sexpdata, "loads", lambda t: calls.append(1) or real(t))
    symbol_lib._lib_cache.clear()
    symbol_lib.load_symbol_def(lib, "Resistor")
    symbol_lib.load_symbol_def(lib, "R_Small")
    assert len(calls) == 1
    lib.write_text(MINI_LIB.read_text() + "\n")  # size changes -> stale entry dropped
    symbol_lib.load_symbol_def(lib, "Resistor")
    assert len(calls) == 2


def test_cached_library_is_not_corrupted_by_callers(tmp_path):
    lib = tmp_path / "L.kicad_sym"
    lib.write_text(MINI_LIB.read_text())
    first = symbol_lib.load_symbol_def(lib, "Resistor")
    first[1] = "MUTATED"
    assert symbol_lib.load_symbol_def(lib, "Resistor")[1] == "Resistor"


@pytest.mark.parametrize("ref", ["U_LATCH", "TP_VCC", "R", "1U"])
def test_reference_without_trailing_number_is_refused(mcp, ref):
    with pytest.raises(ValueError, match="no trailing number"):
        call(mcp, "add_symbol", lib_id="MiniLib:Resistor", reference=ref, value="1k",
             x_mm=101.6, y_mm=101.6)


@pytest.mark.parametrize("ref", [
    "R1", "U_LATCH1", "TP_VCC2", "#PWR0001", "R?",
    "U_BUCK5V1", "U_BUCK1V81", "Q_OD_MODE21",  # digits inside the prefix are fine
])
def test_annotatable_references_are_accepted(mcp, ref):
    call(mcp, "add_symbol", lib_id="MiniLib:Resistor", reference=ref, value="1k",
         x_mm=101.6, y_mm=101.6)


def test_cli_errors_drop_fontconfig_noise():
    from kicad_claude.adapters import kicad_cli

    noise = "\n".join(f'Fontconfig warning: "x", line {i}: bad' for i in range(40))
    assert kicad_cli._clean_stderr(noise + "\nError loading schematic") == "Error loading schematic"
