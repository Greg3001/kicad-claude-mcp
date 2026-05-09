"""Phase 3 — schematic editing.

Strategy:
- Unit tests for geometry, sch_io, sch_editor that don't need KiCAD.
- Integration tests that build a real schematic, write it, then verify with
  kicad-cli (marked @pytest.mark.slow).
"""

from __future__ import annotations

import math
import shutil
import subprocess
from pathlib import Path

import pytest

from kicad_claude import state
from kicad_claude.adapters import sch_editor as ed
from kicad_claude.adapters import sch_io
from kicad_claude.indexer import kicad_libs
from kicad_claude.templates.blank import write_blank_project
from kicad_claude.tools import library as lib_tools
from kicad_claude.tools import schematic as sch_tools
from kicad_claude.utils.geometry import (
    mcp_to_kicad_xy,
    normalize_rotation,
    rotate_xy,
)
from kicad_claude.utils.kicad_paths import find_kicad_cli, find_symbol_lib_dirs

FIXTURES = Path(__file__).parent / "fixtures"


# ===== Geometry ============================================================ #


def test_mcp_to_kicad_y_flip_round_trip():
    x_mcp, y_mcp = 100.0, 30.0
    x_k, y_k = mcp_to_kicad_xy(x_mcp, y_mcp, page_height_mm=210.0)
    assert (x_k, y_k) == (100.0, 180.0)  # 210 - 30
    # Inverse: same function, since y -> H - (H - y) = y
    from kicad_claude.utils.geometry import kicad_to_mcp_xy
    assert kicad_to_mcp_xy(*mcp_to_kicad_xy(50, 70)) == (50.0, 70.0)


def test_normalize_rotation_accepts_right_angles():
    for r in (0, 90, 180, 270, 360, -90):
        normalize_rotation(r)


def test_normalize_rotation_rejects_other_angles():
    with pytest.raises(ValueError):
        normalize_rotation(45)


def test_rotate_xy_90_ccw():
    x, y = rotate_xy(1, 0, 90)
    assert math.isclose(x, 0, abs_tol=1e-9)
    assert math.isclose(y, 1, abs_tol=1e-9)


# ===== sch_io pretty-printer =============================================== #


def test_pretty_print_inline_atoms():
    import sexpdata

    node = [sexpdata.Symbol("at"), 39.37, 29.21, 0]
    assert sch_io.dumps(node) == "(at 39.37 29.21 0)"


def test_pretty_print_multiline_when_list_children():
    import sexpdata

    node = [
        sexpdata.Symbol("symbol"),
        "Foo",
        [sexpdata.Symbol("at"), 0, 0, 0],
        [sexpdata.Symbol("uuid"), "abc"],
    ]
    out = sch_io.dumps(node)
    assert "(symbol \"Foo\"\n" in out
    assert "\t(at 0 0 0)" in out
    assert "\t(uuid \"abc\")" in out


def test_pretty_print_round_trip_blank_project(tmp_path: Path):
    files = write_blank_project(tmp_path, "p")
    tree = sch_io.parse_file(files["sch"])
    out = tmp_path / "rt.kicad_sch"
    sch_io.write_file(out, tree)
    # Re-parse our own output — should give an equivalent tree.
    tree2 = sch_io.parse_file(out)
    assert sch_io.dumps(tree) == sch_io.dumps(tree2)


# ===== sch_editor on the blank template (no library lookups) =============== #


@pytest.fixture
def blank_project(tmp_path: Path):
    state.clear_active()
    files = write_blank_project(tmp_path / "demo", "demo")
    state.set_active(tmp_path / "demo", "demo")
    yield files
    state.clear_active()


def test_add_wire_then_round_trip(blank_project):
    sch_path = blank_project["sch"]
    tree = sch_io.parse_file(sch_path)
    ed.add_wire(tree, 50, 50, 80, 50)
    sch_io.write_file(sch_path, tree)
    tree2 = sch_io.parse_file(sch_path)
    wires = sch_io.find_children(tree2, "wire")
    assert len(wires) == 1


def test_add_label_then_read_back(blank_project):
    sch_path = blank_project["sch"]
    tree = sch_io.parse_file(sch_path)
    ed.add_label(tree, "VBUS", 60, 100, "right")
    sch_io.write_file(sch_path, tree)
    tree2 = sch_io.parse_file(sch_path)
    labels = sch_io.find_children(tree2, "label")
    assert len(labels) == 1
    assert labels[0][1] == "VBUS"


def test_backup_creates_dot_backups_dir(blank_project):
    sch_path = blank_project["sch"]
    backup = ed.backup_file(sch_path)
    assert backup is not None
    assert backup.parent.name == ".backups"
    assert backup.is_file()


# ===== add_symbol with a fixture lib (no real KiCAD libs needed) =========== #


def _patched_index_with_minilib(tmp_path):
    """Build an index pointing only at our MiniLib.kicad_sym fixture."""
    sym_dir = tmp_path / "syms"
    sym_dir.mkdir()
    shutil.copy(FIXTURES / "MiniLib.kicad_sym", sym_dir / "MiniLib.kicad_sym")
    return kicad_libs.build_index(symbol_dirs=[sym_dir], footprint_dirs=[])


def test_add_symbol_via_editor(blank_project, tmp_path, monkeypatch):
    sch_path = blank_project["sch"]
    tree = sch_io.parse_file(sch_path)

    # Fetch lib symbol def from the fixture.
    sym_def = ed.fetch_symbol_def(FIXTURES / "MiniLib.kicad_sym", "Resistor")
    ed.add_symbol(
        tree,
        qualified_lib_id="MiniLib:Resistor",
        reference="R1",
        value="10k",
        x_mm=100,
        y_mm=80,
        rotation=0,
        sym_def_node=sym_def,
        project_name="demo",
    )
    sch_io.write_file(sch_path, tree)

    tree2 = sch_io.parse_file(sch_path)
    s = ed.find_symbol_by_reference(tree2, "R1")
    assert s is not None
    assert ed.get_symbol_property(s, "Value") == "10k"
    # lib_symbols injected
    assert ed.find_lib_symbol_def(tree2, "MiniLib:Resistor") is not None


def test_get_pin_position_for_symbol_at_origin(blank_project):
    sch_path = blank_project["sch"]
    tree = sch_io.parse_file(sch_path)
    sym_def = ed.fetch_symbol_def(FIXTURES / "MiniLib.kicad_sym", "Resistor")
    # Our MiniLib resistor has pin1 at local (0, 2.54) and pin2 at (0, -2.54).
    # Symbol Y is "down" in lib coords.
    ed.add_symbol(
        tree,
        qualified_lib_id="MiniLib:Resistor",
        reference="R1",
        value="10k",
        x_mm=100,
        y_mm=100,
        rotation=0,
        sym_def_node=sym_def,
        project_name="demo",
    )
    pins = ed.list_pins_for_symbol(tree, "R1")
    assert len(pins) == 2
    by_num = {p["number"]: p for p in pins}
    # In MCP coords (Y up), the lib pin at lib-y=2.54 is BELOW symbol origin
    # (KiCAD lib Y is "down"), so MCP-y < 100. Pin at lib-y=-2.54 is above.
    assert by_num["1"]["position_mm"][0] == 100.0
    assert by_num["2"]["position_mm"][0] == 100.0
    # Symmetry: pin1 and pin2 mirror around symbol y
    y1 = by_num["1"]["position_mm"][1]
    y2 = by_num["2"]["position_mm"][1]
    assert math.isclose(y1 + y2, 200.0, abs_tol=0.01)  # 2 * symbol_y_mcp


def test_remove_symbol_returns_false_when_missing(blank_project):
    tree = sch_io.parse_file(blank_project["sch"])
    assert ed.remove_symbol(tree, "DOES_NOT_EXIST") is False


def test_duplicate_reference_rejected(blank_project):
    sch_path = blank_project["sch"]
    tree = sch_io.parse_file(sch_path)
    sym_def = ed.fetch_symbol_def(FIXTURES / "MiniLib.kicad_sym", "Resistor")
    ed.add_symbol(
        tree,
        qualified_lib_id="MiniLib:Resistor",
        reference="R1",
        value="10k",
        x_mm=100,
        y_mm=80,
        rotation=0,
        sym_def_node=sym_def,
        project_name="demo",
    )
    # Second R1 should fail — even with a fresh def.
    sym_def2 = ed.fetch_symbol_def(FIXTURES / "MiniLib.kicad_sym", "Resistor")
    with pytest.raises(ValueError, match="already exists"):
        ed.add_symbol(
            tree,
            qualified_lib_id="MiniLib:Resistor",
            reference="R1",
            value="other",
            x_mm=120,
            y_mm=80,
            rotation=0,
            sym_def_node=sym_def2,
            project_name="demo",
        )


# ===== Tool layer (via FastMCP) ============================================ #


def _make_mcp_with_fixture_index(monkeypatch, tmp_path):
    """Patch lib_tools to expose the MiniLib fixture as the only indexed lib."""
    from mcp.server.fastmcp import FastMCP

    idx = _patched_index_with_minilib(tmp_path)
    monkeypatch.setattr(lib_tools, "load_cache", lambda: idx)
    monkeypatch.setattr(lib_tools, "_index", None)

    mcp = FastMCP("test")
    sch_tools.register(mcp)
    lib_tools.register(mcp)
    return mcp


def _call(mcp, name, **kwargs):
    return mcp._tool_manager.get_tool(name).fn(**kwargs)


def test_add_symbol_tool_writes_schematic(blank_project, tmp_path, monkeypatch):
    mcp = _make_mcp_with_fixture_index(monkeypatch, tmp_path)
    res = _call(
        mcp,
        "add_symbol",
        lib_id="MiniLib:Resistor",
        reference="R1",
        value="10k",
        x_mm=100,
        y_mm=80,
    )
    assert res["reference"] == "R1"
    assert res["pin_count"] == 2
    # Read the schematic back to confirm persisted
    tree = sch_io.parse_file(blank_project["sch"])
    assert ed.find_symbol_by_reference(tree, "R1") is not None


def test_get_pin_position_tool(blank_project, tmp_path, monkeypatch):
    mcp = _make_mcp_with_fixture_index(monkeypatch, tmp_path)
    _call(mcp, "add_symbol", lib_id="MiniLib:Resistor", reference="R1", value="10k", x_mm=100, y_mm=100)
    res = _call(mcp, "get_pin_position", reference="R1", pin="1")
    assert res["reference"] == "R1"
    assert res["pin"] == "1"
    assert res["position_mm"][0] == 100.0


def test_move_then_remove_via_tools(blank_project, tmp_path, monkeypatch):
    mcp = _make_mcp_with_fixture_index(monkeypatch, tmp_path)
    _call(mcp, "add_symbol", lib_id="MiniLib:Resistor", reference="R1", value="10k", x_mm=100, y_mm=100)
    _call(mcp, "move_symbol", reference="R1", x_mm=50, y_mm=60, rotation=90)
    tree = sch_io.parse_file(blank_project["sch"])
    s = ed.find_symbol_by_reference(tree, "R1")
    at = sch_io.find_child(s, "at")
    # In KiCAD coords: x=50, y=210-60=150, rot=90
    assert at[1] == 50.0
    assert at[2] == 150.0
    assert at[3] == 90
    # Now remove
    _call(mcp, "remove_symbol", reference="R1")
    tree = sch_io.parse_file(blank_project["sch"])
    assert ed.find_symbol_by_reference(tree, "R1") is None


# ===== Acceptance: voltage divider (slow, real KiCAD libs) ================= #


@pytest.mark.slow
def test_voltage_divider_acceptance(tmp_path):
    """End-to-end: build a 10k/1k divider between +5V and GND. kicad-cli must parse."""
    cli = find_kicad_cli()
    if not cli or not find_symbol_lib_dirs():
        pytest.skip("no kicad-cli or KiCAD symbol libs available")

    # Need a real library index (from cache if it exists, else build it).
    cached = kicad_libs.load_cache()
    if cached is None:
        pytest.skip("library index not built; run `index_libraries` first")
    state.clear_active()
    lib_tools._index = cached  # warm tool memo

    files = write_blank_project(tmp_path / "divider", "divider")
    state.set_active(tmp_path / "divider", "divider")

    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP("test")
    sch_tools.register(mcp)
    lib_tools.register(mcp)

    # Layout: vertical strip at x=100, +5V at top (y=160), GND at bottom (y=40).
    _call(mcp, "add_power_symbol", net="+5V", x_mm=100, y_mm=160)
    _call(mcp, "add_symbol", lib_id="Device:R", reference="R1", value="10k",
          x_mm=100, y_mm=130)
    _call(mcp, "add_symbol", lib_id="Device:R", reference="R2", value="1k",
          x_mm=100, y_mm=80)
    _call(mcp, "add_power_symbol", net="GND", x_mm=100, y_mm=40)

    # Wire R1 between +5V and the mid-node
    _call(mcp, "add_wire", x1_mm=100, y1_mm=160, x2_mm=100, y2_mm=140)  # +5V → R1.top
    _call(mcp, "add_wire", x1_mm=100, y1_mm=120, x2_mm=100, y2_mm=90)   # R1.bot → R2.top
    _call(mcp, "add_wire", x1_mm=100, y1_mm=70, x2_mm=100, y2_mm=40)    # R2.bot → GND

    sch_path = files["sch"]
    state.clear_active()

    # kicad-cli must parse the file (returncode 0). ERC violations are OK.
    r = subprocess.run(
        [str(cli), "sch", "erc", str(sch_path)],
        capture_output=True, text=True, timeout=60,
        cwd=tmp_path,
    )
    assert r.returncode == 0, f"erc failed: stderr={r.stderr}"
