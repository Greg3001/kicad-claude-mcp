"""Phase 13 — STEP export, custom DRC rules, multi-board, symbol/footprint editor.

Strategy:
- Pure-Python tests for drc_rules and library_create.
- Tool tests with synthetic indices.
- @pytest.mark.slow: real kicad-cli runs against generated artifacts.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import sexpdata

from kicad_claude import state
from kicad_claude.adapters import drc_rules, library_create as lc, sch_io
from kicad_claude.indexer import kicad_libs
from kicad_claude.templates.blank import write_blank_pcb, write_blank_project
from kicad_claude.tools import library as lib_tools
from kicad_claude.utils.kicad_paths import find_kicad_cli


# ===== Custom DRC rules ==================================================== #


def test_render_rule_basic():
    rule = {
        "name": "Power clearance",
        "constraint_type": "clearance",
        "min_value": 0.5,
        "condition": "A.NetClass == 'Power'",
        "severity": "error",
    }
    out = drc_rules.render_rule(rule)
    assert '(rule "Power clearance"' in out
    assert "(constraint clearance (min 0.5mm))" in out
    assert '(condition "A.NetClass == \'Power\'")' in out
    assert "(severity error)" in out


def test_validate_rule_rejects_invalid_constraint():
    with pytest.raises(ValueError, match="constraint_type"):
        drc_rules.validate_rule({
            "name": "x", "constraint_type": "bogus", "severity": "error",
        })


def test_validate_rule_rejects_invalid_severity():
    with pytest.raises(ValueError, match="severity"):
        drc_rules.validate_rule({
            "name": "x", "constraint_type": "clearance", "severity": "fatal",
        })


def test_validate_rule_rejects_empty_name():
    with pytest.raises(ValueError, match="name"):
        drc_rules.validate_rule({
            "name": "", "constraint_type": "clearance", "severity": "error",
        })


def test_write_and_read_rules_round_trip(tmp_path: Path):
    rules = [
        {
            "name": "rule_a", "constraint_type": "clearance",
            "min_value": 0.3, "condition": "A.Net == '+5V'",
            "severity": "error",
        },
        {
            "name": "rule_b", "constraint_type": "track_width",
            "min_value": 0.5, "condition": "A.NetClass == 'Power'",
            "severity": "warning",
        },
    ]
    dru_path = tmp_path / "test.kicad_dru"
    drc_rules.write_rules(dru_path, rules)
    assert dru_path.is_file()
    parsed = drc_rules.read_rules(dru_path)
    assert len(parsed) == 2
    assert parsed[0]["name"] == "rule_a"
    assert parsed[1]["constraint_type"] == "track_width"


def test_read_rules_returns_empty_for_missing_file(tmp_path: Path):
    assert drc_rules.read_rules(tmp_path / "nope.kicad_dru") == []


# ===== DRC rules tools ===================================================== #


def _make_rules_mcp(tmp_path: Path):
    state.clear_active()
    files = write_blank_project(tmp_path / "p", "p")
    state.set_active(tmp_path / "p", "p")

    from mcp.server.fastmcp import FastMCP
    from kicad_claude.tools import rules as rules_tools
    mcp = FastMCP("t")
    rules_tools.register(mcp)
    return mcp, files


def _call(mcp, _name, **kw):
    return mcp._tool_manager.get_tool(_name).fn(**kw)


def test_add_drc_rule_writes_to_dru_file(tmp_path: Path):
    mcp, files = _make_rules_mcp(tmp_path)
    res = _call(
        mcp, "add_drc_rule",
        name="usb_clearance",
        constraint_type="clearance", min_value_mm=0.3,
        condition="A.NetClass == 'USB'",
    )
    assert res["total_rules"] == 1
    dru_text = Path(res["dru_path"]).read_text()
    assert "usb_clearance" in dru_text
    state.clear_active()


def test_add_drc_rule_replaces_same_name(tmp_path: Path):
    mcp, _ = _make_rules_mcp(tmp_path)
    _call(mcp, "add_drc_rule", name="x", constraint_type="clearance", min_value_mm=0.2)
    _call(mcp, "add_drc_rule", name="x", constraint_type="clearance", min_value_mm=0.5)
    res = _call(mcp, "list_drc_rules")
    assert res["total"] == 1
    assert res["rules"][0]["min_value"] == "0.5mm"
    state.clear_active()


def test_remove_drc_rule(tmp_path: Path):
    mcp, _ = _make_rules_mcp(tmp_path)
    _call(mcp, "add_drc_rule", name="x", constraint_type="clearance", min_value_mm=0.2)
    _call(mcp, "remove_drc_rule", name="x")
    assert _call(mcp, "list_drc_rules")["total"] == 0
    with pytest.raises(KeyError):
        _call(mcp, "remove_drc_rule", name="x")
    state.clear_active()


def test_clear_drc_rules(tmp_path: Path):
    mcp, _ = _make_rules_mcp(tmp_path)
    _call(mcp, "add_drc_rule", name="a", constraint_type="clearance", min_value_mm=0.2)
    _call(mcp, "add_drc_rule", name="b", constraint_type="track_width", min_value_mm=0.5)
    _call(mcp, "clear_drc_rules")
    assert _call(mcp, "list_drc_rules")["total"] == 0
    state.clear_active()


def test_add_drc_rule_unknown_constraint_raises(tmp_path: Path):
    mcp, _ = _make_rules_mcp(tmp_path)
    with pytest.raises(ValueError, match="constraint_type"):
        _call(mcp, "add_drc_rule", name="x", constraint_type="bogus", min_value_mm=0.1)
    state.clear_active()


# ===== Multi-board ========================================================= #


def _make_pcb_mcp(tmp_path):
    cached = kicad_libs.load_cache()
    if cached is None:
        pytest.skip("library index not built")
    from mcp.server.fastmcp import FastMCP
    from kicad_claude.tools import pcb as pcb_tools
    state.clear_active()
    files = write_blank_project(tmp_path / "p", "p")
    state.set_active(tmp_path / "p", "p")
    lib_tools._index = cached
    mcp = FastMCP("t")
    pcb_tools.register(mcp)
    return mcp, files


def test_add_board_creates_kicad_pcb(tmp_path: Path):
    mcp, files = _make_pcb_mcp(tmp_path)
    res = _call(mcp, "add_board", name="breakout")
    assert res["filename"] == "breakout.kicad_pcb"
    assert Path(res["path"]).is_file()
    assert res["active"] is True
    # Project file lists it
    pro = json.loads(files["pro"].read_text())
    assert "breakout.kicad_pcb" in pro.get("boards", [])
    state.clear_active()


def test_list_boards_finds_added(tmp_path: Path):
    mcp, _ = _make_pcb_mcp(tmp_path)
    _call(mcp, "add_board", name="b1")
    _call(mcp, "add_board", name="b2")
    res = _call(mcp, "list_boards")
    assert "b1.kicad_pcb" in res["boards"]
    assert "b2.kicad_pcb" in res["boards"]
    assert res["main_board"] == "p.kicad_pcb"
    state.clear_active()


def test_set_active_board_switches_target(tmp_path: Path):
    mcp, _ = _make_pcb_mcp(tmp_path)
    _call(mcp, "add_board", name="alt")
    _call(mcp, "set_active_board", filename="alt.kicad_pcb")
    res = _call(mcp, "list_boards")
    assert res["active_board"] == "alt.kicad_pcb"
    _call(mcp, "set_active_board", filename="")  # back to main
    res = _call(mcp, "list_boards")
    assert res["active_board"] == "p.kicad_pcb"
    state.clear_active()


def test_add_board_refuses_duplicate(tmp_path: Path):
    mcp, _ = _make_pcb_mcp(tmp_path)
    _call(mcp, "add_board", name="dup")
    with pytest.raises(FileExistsError):
        _call(mcp, "add_board", name="dup")
    state.clear_active()


def test_state_set_active_board_validates_existence(tmp_path: Path):
    state.clear_active()
    files = write_blank_project(tmp_path / "p", "p")
    state.set_active(tmp_path / "p", "p")
    with pytest.raises(FileNotFoundError):
        state.set_active_board("doesnotexist.kicad_pcb")
    state.clear_active()


# ===== Symbol creation ===================================================== #


def test_build_symbol_node_includes_pins(tmp_path: Path):
    node = lc.build_symbol_node(
        qualified_lib_id="vendor:TEST",
        pins=[
            {"number": "1", "name": "VCC", "x_mm": 0, "y_mm": 5, "angle_deg": 270, "type": "power_in"},
            {"number": "2", "name": "GND", "x_mm": 0, "y_mm": -5, "angle_deg": 90, "type": "power_in"},
        ],
        body_width_mm=4, body_height_mm=8,
        reference_prefix="U", value="TEST",
    )
    # In a .kicad_sym source file the symbol name is BARE (not Lib:Name).
    # KiCAD adds the qualifier when the symbol is copied into a schematic's
    # lib_symbols block, not in the source library.
    assert node[1] == "TEST"
    # Sub-symbol with pins
    sub = sch_io.find_child(node, "symbol")
    assert sub is not None
    pins_in_sub = sch_io.find_children(sub, "pin")
    assert len(pins_in_sub) == 2


def test_create_symbol_writes_lib_and_table(tmp_path: Path):
    state.clear_active()
    files = write_blank_project(tmp_path / "p", "p")
    state.set_active(tmp_path / "p", "p")

    res = lc.create_symbol(
        tmp_path / "p",
        lib_name="custom", symbol_name="WIDGET",
        pins=[
            {"number": "1", "name": "A", "x_mm": -3, "y_mm": 0, "angle_deg": 180, "type": "passive"},
            {"number": "2", "name": "B", "x_mm": 3,  "y_mm": 0, "angle_deg": 0,   "type": "passive"},
        ],
    )
    assert res["lib_id"] == "custom:WIDGET"
    lib_path = Path(res["lib_path"])
    assert lib_path.is_file()
    # Re-parse to confirm it's a valid kicad_symbol_lib
    data = sexpdata.loads(lib_path.read_text())
    assert str(data[0]) == "kicad_symbol_lib"
    syms = [c for c in data if isinstance(c, list) and len(c) >= 2 and isinstance(c[0], sexpdata.Symbol) and str(c[0]) == "symbol"]
    # Bare name in source lib (the indexer prepends "custom:" automatically)
    assert any(s[1] == "WIDGET" for s in syms)
    # sym-lib-table registered
    sym_table = (tmp_path / "p" / "sym-lib-table").read_text()
    assert "custom" in sym_table
    state.clear_active()


def test_create_symbol_rejects_duplicate(tmp_path: Path):
    state.clear_active()
    files = write_blank_project(tmp_path / "p", "p")
    state.set_active(tmp_path / "p", "p")
    pins = [{"number": "1", "name": "A", "x_mm": -3, "y_mm": 0, "angle_deg": 180, "type": "passive"}]
    lc.create_symbol(tmp_path / "p", lib_name="lib", symbol_name="S", pins=pins)
    with pytest.raises(FileExistsError):
        lc.create_symbol(tmp_path / "p", lib_name="lib", symbol_name="S", pins=pins)
    state.clear_active()


def test_create_symbol_validates_pin_type(tmp_path: Path):
    state.clear_active()
    files = write_blank_project(tmp_path / "p", "p")
    state.set_active(tmp_path / "p", "p")
    with pytest.raises(ValueError, match="pin_type"):
        lc.create_symbol(
            tmp_path / "p", lib_name="lib", symbol_name="S",
            pins=[{"number": "1", "x_mm": 0, "y_mm": 0, "type": "bogus"}],
        )
    state.clear_active()


# ===== Footprint creation ================================================== #


def test_create_footprint_writes_kicad_mod(tmp_path: Path):
    state.clear_active()
    files = write_blank_project(tmp_path / "p", "p")
    state.set_active(tmp_path / "p", "p")
    res = lc.create_footprint(
        tmp_path / "p",
        lib_name="custom", footprint_name="MYPAD",
        pads=[
            {"number": "1", "type": "smd", "shape": "rect",
             "x_mm": -1, "y_mm": 0, "size_x_mm": 1.5, "size_y_mm": 0.8},
            {"number": "2", "type": "smd", "shape": "rect",
             "x_mm":  1, "y_mm": 0, "size_x_mm": 1.5, "size_y_mm": 0.8},
        ],
        description="Test 2-pad",
    )
    assert res["lib_id"] == "custom:MYPAD"
    mod = Path(res["kicad_mod_path"])
    assert mod.is_file()
    data = sexpdata.loads(mod.read_text())
    assert str(data[0]) == "footprint"
    assert data[1] == "MYPAD"
    pads = sch_io.find_children(data, "pad")
    assert len(pads) == 2
    # Auto-courtyard on F.CrtYd
    crtyd_lines = [
        n for n in sch_io.find_children(data, "fp_line")
        if (sch_io.find_child(n, "layer") or [None, ""])[1] == "F.CrtYd"
    ]
    assert len(crtyd_lines) == 4
    # fp-lib-table updated
    fp_table = (tmp_path / "p" / "fp-lib-table").read_text()
    assert "custom" in fp_table
    state.clear_active()


def test_create_footprint_thru_hole_default_drill(tmp_path: Path):
    state.clear_active()
    files = write_blank_project(tmp_path / "p", "p")
    state.set_active(tmp_path / "p", "p")
    res = lc.create_footprint(
        tmp_path / "p",
        lib_name="lib", footprint_name="THT",
        pads=[
            {"number": "1", "type": "thru_hole", "shape": "circle",
             "x_mm": 0, "y_mm": 0, "size_x_mm": 1.6, "size_y_mm": 1.6},
        ],
    )
    mod = Path(res["kicad_mod_path"])
    data = sexpdata.loads(mod.read_text())
    pad = sch_io.find_children(data, "pad")[0]
    drill = sch_io.find_child(pad, "drill")
    # Default drill = max(0.3, min(sx, sy) - 0.4) = max(0.3, 1.2) = 1.2
    assert drill is not None
    assert math_isclose(float(drill[1]), 1.2, abs_tol=0.01)
    state.clear_active()


def math_isclose(a, b, abs_tol=1e-9):
    import math
    return math.isclose(a, b, abs_tol=abs_tol)


def test_create_footprint_validates_pad_type(tmp_path: Path):
    state.clear_active()
    files = write_blank_project(tmp_path / "p", "p")
    state.set_active(tmp_path / "p", "p")
    with pytest.raises(ValueError, match="pad_type"):
        lc.create_footprint(
            tmp_path / "p", lib_name="lib", footprint_name="X",
            pads=[{"number": "1", "type": "weird", "shape": "rect",
                   "x_mm": 0, "y_mm": 0, "size_x_mm": 1, "size_y_mm": 1}],
        )
    state.clear_active()


# ===== Slow acceptance (real kicad-cli) ==================================== #


@pytest.mark.slow
def test_acceptance_step_export(tmp_path: Path):
    if find_kicad_cli() is None:
        pytest.skip("kicad-cli not available")
    cached = kicad_libs.load_cache()
    if cached is None:
        pytest.skip("library index not built")
    state.clear_active()
    lib_tools._index = cached
    files = write_blank_project(tmp_path / "p", "p")
    state.set_active(tmp_path / "p", "p")

    from mcp.server.fastmcp import FastMCP
    from kicad_claude.tools import (
        library, manufacturing, pcb as pcb_tools,
    )
    mcp = FastMCP("t")
    for mod in (library, manufacturing, pcb_tools):
        mod.register(mcp)
    def call(_tool, **kw):
        return mcp._tool_manager.get_tool(_tool).fn(**kw)

    call("set_board_outline", width_mm=50, height_mm=30)
    call("add_footprint", lib_id="Resistor_SMD:R_0603_1608Metric",
         reference="R1", value="10k", x_mm=20, y_mm=15)
    res = call("export_step_3d", include_components=False)
    assert Path(res["output_path"]).is_file()
    assert res["size_bytes"] > 1024
    state.clear_active()


@pytest.mark.slow
def test_acceptance_drc_with_custom_dru(tmp_path: Path):
    """Custom .kicad_dru rule fires when the condition matches."""
    if find_kicad_cli() is None:
        pytest.skip("kicad-cli not available")
    cached = kicad_libs.load_cache()
    if cached is None:
        pytest.skip("library index not built")

    state.clear_active()
    lib_tools._index = cached
    files = write_blank_project(tmp_path / "p", "p")
    state.set_active(tmp_path / "p", "p")

    from mcp.server.fastmcp import FastMCP
    from kicad_claude.tools import (
        library, pcb as pcb_tools, rules as rules_tools, validation,
    )
    mcp = FastMCP("t")
    for mod in (library, pcb_tools, rules_tools, validation):
        mod.register(mcp)
    def call(_tool, **kw):
        return mcp._tool_manager.get_tool(_tool).fn(**kw)

    call("set_board_outline", width_mm=50, height_mm=30)
    # Add a track wider than 5mm — should be fine
    call("add_track", x1_mm=10, y1_mm=10, x2_mm=40, y2_mm=10, width_mm=0.25)
    # Custom rule: track_width must be >= 1mm everywhere
    call(
        "add_drc_rule",
        name="any_track_min_1mm",
        constraint_type="track_width",
        min_value_mm=1.0,
        condition="A.Layer == 'F.Cu'",
        severity="error",
    )
    drc = call("run_drc", schematic_parity=False)
    # Expect at least one violation about track width
    descs = " ".join(v.get("description", "") for v in drc["violations"])
    assert drc["errors"] >= 1, f"expected width violation, got: {descs}"
    state.clear_active()


@pytest.mark.slow
def test_acceptance_custom_symbol_and_footprint_parse(tmp_path: Path):
    """The .kicad_sym and .kicad_mod we generate must be readable by KiCAD."""
    if find_kicad_cli() is None:
        pytest.skip("kicad-cli not available")
    cached = kicad_libs.load_cache()
    if cached is None:
        pytest.skip("library index not built")
    state.clear_active()
    lib_tools._index = cached

    files = write_blank_project(tmp_path / "p", "p")
    state.set_active(tmp_path / "p", "p")

    # Create a symbol + a footprint
    lc.create_symbol(
        tmp_path / "p",
        lib_name="custom", symbol_name="OPAMP",
        pins=[
            {"number": "1", "name": "OUT", "x_mm": 5, "y_mm": 0, "angle_deg": 0, "type": "output"},
            {"number": "2", "name": "VCC", "x_mm": 0, "y_mm": 5, "angle_deg": 270, "type": "power_in"},
            {"number": "3", "name": "GND", "x_mm": 0, "y_mm": -5, "angle_deg": 90, "type": "power_in"},
            {"number": "4", "name": "IN+", "x_mm": -5, "y_mm": 1.27, "angle_deg": 180, "type": "input"},
            {"number": "5", "name": "IN-", "x_mm": -5, "y_mm": -1.27, "angle_deg": 180, "type": "input"},
        ],
        description="Test opamp", keywords="test custom",
    )
    lc.create_footprint(
        tmp_path / "p",
        lib_name="custom", footprint_name="MY_SOIC",
        pads=[
            {"number": str(i), "type": "smd", "shape": "rect",
             "x_mm": -2 if i <= 4 else 2,
             "y_mm": (i - 1) * 1.27 - 1.905 if i <= 4 else (8 - i) * 1.27 - 1.905,
             "size_x_mm": 1.5, "size_y_mm": 0.6}
            for i in range(1, 9)
        ],
        description="Test SOIC-8",
    )
    # Schematic + PCB should still pass kicad-cli ERC/DRC (the libs aren't
    # used by the empty schematic, but they shouldn't break parsing).
    import subprocess
    cli = find_kicad_cli()
    r1 = subprocess.run(
        [str(cli), "sch", "erc", "--format", "json", str(files["sch"])],
        capture_output=True, text=True, timeout=30, cwd=tmp_path,
    )
    assert r1.returncode == 0
    r2 = subprocess.run(
        [str(cli), "pcb", "drc", "--format", "json", str(files["pcb"])],
        capture_output=True, text=True, timeout=30, cwd=tmp_path,
    )
    assert r2.returncode == 0
    state.clear_active()
