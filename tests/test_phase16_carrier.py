"""Phase 16 — fixes found while preparing a large SOM-carrier schematic.

See docs/AUDIT_2026-10.md for the defect list. Pure-Python tests run
everywhere; the kicad-cli round trips are skipped when KiCad is absent.
"""

from __future__ import annotations

import json
import subprocess
from collections import Counter
from pathlib import Path

import pytest

from kicad_claude.adapters import sch_editor as ed
from kicad_claude.adapters import sch_io, symbol_lib
import shutil

from mcp.server.fastmcp import FastMCP

from kicad_claude import state
from kicad_claude.indexer import kicad_libs
from kicad_claude.tools import library as lib_tools
from kicad_claude.tools import schematic as sch_tools
from kicad_claude.templates.blank import write_blank_project
from kicad_claude.utils.kicad_paths import find_kicad_cli

FIXTURES = Path(__file__).parent / "fixtures"
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


# ===== helpers: real KiCad round trips ===================================== #

KICAD_CLI = find_kicad_cli()
needs_kicad = pytest.mark.skipif(KICAD_CLI is None, reason="kicad-cli not installed")


@pytest.fixture
def blank_project(tmp_path: Path):
    state.clear_active()
    files = write_blank_project(tmp_path / "demo", "demo")
    state.set_active(tmp_path / "demo", "demo")
    yield files
    state.clear_active()


def erc_counts(sch_path: Path) -> Counter:
    """Run kicad-cli ERC and count violations by type across all sheets."""
    out = sch_path.with_suffix(".erc.json")
    subprocess.run(
        [str(KICAD_CLI), "sch", "erc", "--format", "json", "--severity-all",
         "-o", str(out), str(sch_path)],
        capture_output=True, text=True, timeout=120, check=False,
    )
    report = json.loads(out.read_text())
    return Counter(
        v["type"] for sh in report.get("sheets", []) for v in sh.get("violations", [])
    )


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


@pytest.fixture
def mcp(blank_project, tmp_path, monkeypatch):
    """FastMCP with schematic tools over a fixture index (MiniLib, UnitLib, power)."""
    syms = tmp_path / "syms"
    syms.mkdir()
    for lib in FIXTURES.glob("*.kicad_sym"):
        shutil.copy(lib, syms / lib.name)
    idx = kicad_libs.build_index(symbol_dirs=[syms], footprint_dirs=[])
    monkeypatch.setattr(lib_tools, "load_cache", lambda: idx)
    monkeypatch.setattr(lib_tools, "_index", None)
    server = FastMCP("test")
    sch_tools.register(server)
    lib_tools.register(server)
    return server


def call(server, name: str, **kwargs):
    return server._tool_manager.get_tool(name).fn(**kwargs)


# ===== project-wide power references ======================================= #


def _refs_on(path: Path) -> list[str]:
    return ed.all_references(sch_io.parse_file(path))


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
        refs += _refs_on(proj / f)
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
