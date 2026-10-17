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
    # In KiCAD coords: x=50, y=208.28-60=148.28 (A4 flip height on the 2.54 grid), rot=90
    assert at[1] == 50.0
    assert at[2] == 148.28
    assert at[3] == 90
    # Now remove
    _call(mcp, "remove_symbol", reference="R1")
    tree = sch_io.parse_file(blank_project["sch"])
    assert ed.find_symbol_by_reference(tree, "R1") is None


# ===== Grid alignment ====================================================== #


def _on_grid(v: float, grid: float = 1.27) -> bool:
    return math.isclose(v / grid, round(v / grid), abs_tol=1e-6)


def test_grid_aligned_height_per_paper():
    from kicad_claude.utils.geometry import grid_aligned_height

    assert grid_aligned_height(210.0) == 208.28  # A4: 82 * 2.54
    assert grid_aligned_height(297.0) == 294.64  # A3
    assert grid_aligned_height(215.9) == 215.9  # USLetter is already on grid


def test_page_height_is_grid_aligned(blank_project):
    tree = sch_io.parse_file(blank_project["sch"])
    assert ed.page_height_mm(tree) == 208.28
    assert _on_grid(ed.page_height_mm(tree), 2.54)


def test_on_grid_mcp_coords_stay_on_kicad_grid(blank_project):
    tree = sch_io.parse_file(blank_project["sch"])
    sym_def = ed.fetch_symbol_def(FIXTURES / "MiniLib.kicad_sym", "Resistor")
    s = ed.add_symbol(
        tree, qualified_lib_id="MiniLib:Resistor", reference="R1", value="10k",
        x_mm=101.6, y_mm=127.0, rotation=0, sym_def_node=sym_def, project_name="demo",
    )
    w = ed.add_wire(tree, 101.6, 129.54, 101.6, 139.7)
    at = sch_io.find_child(s, "at")
    assert _on_grid(at[1]) and _on_grid(at[2])
    for xy in sch_io.find_children(sch_io.find_child(w, "pts"), "xy"):
        assert _on_grid(xy[1]) and _on_grid(xy[2])
    # Pin positions round-trip to the MCP grid too.
    for p in ed.list_pins_for_symbol(tree, "R1"):
        assert all(_on_grid(v) for v in p["position_mm"])


# ===== ERC report shaping ================================================== #


def test_erc_shape_reads_per_sheet_violations(tmp_path):
    from kicad_claude.adapters import kicad_cli

    data = {
        "kicad_version": "10.0.1",
        "sheets": [
            {
                "path": "/",
                "violations": [
                    {"type": "endpoint_off_grid", "severity": "warning", "items": []},
                    {"type": "endpoint_off_grid", "severity": "warning", "items": []},
                    {"type": "pin_not_connected", "severity": "error", "items": []},
                ],
            }
        ],
    }
    res = kicad_cli._shape_erc(data, tmp_path / "x.json")
    assert res["errors"] == 1
    assert res["warnings"] == 2
    assert res["total_violations"] == 3
    assert res["by_type"] == {"endpoint_off_grid": 2, "pin_not_connected": 1}
    assert res["violations"][0]["sheet"] == "/"


# ===== Field placement ===================================================== #


def _prop_at(s_node, name):
    return sch_io.find_child(ed._find_property(s_node, name), "at")


def _prop_justify(s_node, name):
    effects = sch_io.find_child(ed._find_property(s_node, name), "effects")
    j = sch_io.find_child(effects, "justify")
    return str(j[1]) if j else None


def _add_resistor(tree, ref="R1", x=101.6, y=101.6, rot=0):
    sym_def = ed.fetch_symbol_def(FIXTURES / "MiniLib.kicad_sym", "Resistor")
    return ed.add_symbol(
        tree, qualified_lib_id="MiniLib:Resistor", reference=ref, value="10k",
        x_mm=x, y_mm=y, rotation=rot, sym_def_node=sym_def, project_name="demo",
    )


def test_add_symbol_places_fields_beside_vertical_body(blank_project):
    tree = sch_io.parse_file(blank_project["sch"])
    s = _add_resistor(tree)
    sx, sy = sch_io.find_child(s, "at")[1:3]
    ref, val = _prop_at(s, "Reference"), _prop_at(s, "Value")
    # Pins are top/bottom, so text goes to the right, Reference above Value.
    assert ref[1] > sx and val[1] > sx
    assert ref[2] < sy < val[2]
    assert ref[3] == 0 and val[3] == 0
    assert _prop_justify(s, "Reference") == "left"
    assert sch_io.find_child(s, "fields_autoplaced") is not None


def test_add_rotated_symbol_keeps_text_horizontal(blank_project):
    tree = sch_io.parse_file(blank_project["sch"])
    s = _add_resistor(tree, rot=90)
    sx, sy = sch_io.find_child(s, "at")[1:3]
    ref, val = _prop_at(s, "Reference"), _prop_at(s, "Value")
    # Pins now left/right: text goes above the body, centred on it.
    assert ref[1] == sx and val[1] == sx
    assert ref[2] < val[2] < sy
    # Field angles are relative to the symbol: 90 here renders horizontal.
    assert ref[3] == 90 and val[3] == 90
    assert _prop_justify(s, "Value") is None


def test_add_symbol_180_flips_local_justify(blank_project):
    tree = sch_io.parse_file(blank_project["sch"])
    s = _add_resistor(tree, rot=180)
    # Local "right" is drawn as left-justified once KiCAD applies the 180° turn.
    assert _prop_justify(s, "Reference") == "right"
    assert _prop_at(s, "Reference")[1] > sch_io.find_child(s, "at")[1]


def test_move_symbol_carries_fields(blank_project):
    tree = sch_io.parse_file(blank_project["sch"])
    s = _add_resistor(tree)
    before = {n: list(_prop_at(s, n)[1:3]) for n in ("Reference", "Value", "Footprint")}
    ed.move_symbol(tree, "R1", 101.6 + 25.4, 101.6 - 12.7)
    for n, (x, y) in before.items():
        at = _prop_at(s, n)
        assert math.isclose(at[1], x + 25.4, abs_tol=1e-6)
        assert math.isclose(at[2], y + 12.7, abs_tol=1e-6)  # MCP down = KiCAD +y


def test_move_symbol_rotation_replaces_fields(blank_project):
    tree = sch_io.parse_file(blank_project["sch"])
    s = _add_resistor(tree)
    ed.move_symbol(tree, "R1", 50.8, 50.8, rotation=90)
    sx, sy = sch_io.find_child(s, "at")[1:3]
    ref = _prop_at(s, "Reference")
    assert ref[3] == 90 and ref[1] == sx and ref[2] < sy


_POWER_LIB = """(kicad_symbol_lib
	(version 20251024)
	(generator "test")
	(symbol "+5V"
		(power global)
		(property "Reference" "#PWR" (at 0 -3.81 0) (hide yes) (effects (font (size 1.27 1.27))))
		(property "Value" "+5V" (at 0 3.556 0) (effects (font (size 1.27 1.27))))
		(property "Footprint" "" (at 0 0 0) (hide yes) (effects (font (size 1.27 1.27))))
		(property "Datasheet" "" (at 0 0 0) (hide yes) (effects (font (size 1.27 1.27))))
		(property "Description" "Power symbol" (at 0 0 0) (hide yes) (effects (font (size 1.27 1.27))))
		(symbol "+5V_0_1"
			(polyline (pts (xy -0.762 1.27) (xy 0 2.54) (xy 0.762 1.27)))
		)
		(symbol "+5V_1_1"
			(pin power_in line (at 0 0 90) (length 0) (hide yes)
				(name "~" (effects (font (size 1.27 1.27))))
				(number "1" (effects (font (size 1.27 1.27))))
			)
		)
	)
)
"""


def test_power_symbol_hides_reference_and_uses_lib_fields(blank_project, tmp_path):
    lib = tmp_path / "power.kicad_sym"
    lib.write_text(_POWER_LIB)
    tree = sch_io.parse_file(blank_project["sch"])
    s = ed.add_symbol(
        tree, qualified_lib_id="power:+5V", reference="#PWR0001", value="+5V",
        x_mm=101.6, y_mm=101.6, rotation=0,
        sym_def_node=ed.fetch_symbol_def(lib, "+5V"), project_name="demo",
    )
    sx, sy = sch_io.find_child(s, "at")[1:3]
    assert ed._is_hidden(ed._find_property(s, "Reference"))
    assert not ed._is_hidden(ed._find_property(s, "Value"))
    # Value sits where the library puts it: 3.556 mm above the origin.
    val = _prop_at(s, "Value")
    assert val[1] == sx and math.isclose(val[2], sy - 3.556, abs_tol=1e-6)


def test_add_power_symbol_tool_hides_reference(blank_project, tmp_path, monkeypatch):
    from mcp.server.fastmcp import FastMCP

    sym_dir = tmp_path / "syms"
    sym_dir.mkdir()
    (sym_dir / "power.kicad_sym").write_text(_POWER_LIB)
    idx = kicad_libs.build_index(symbol_dirs=[sym_dir], footprint_dirs=[])
    monkeypatch.setattr(lib_tools, "load_cache", lambda: idx)
    monkeypatch.setattr(lib_tools, "_index", None)
    mcp = FastMCP("test")
    sch_tools.register(mcp)
    lib_tools.register(mcp)

    res = _call(mcp, "add_power_symbol", net="+5V", x_mm=101.6, y_mm=101.6)
    tree = sch_io.parse_file(blank_project["sch"])
    s = ed.find_symbol_by_reference(tree, res["reference"])
    assert ed._is_hidden(ed._find_property(s, "Reference"))


# ===== Junctions and fields via tools ====================================== #


def test_add_junction_tool(blank_project, tmp_path, monkeypatch):
    mcp = _make_mcp_with_fixture_index(monkeypatch, tmp_path)
    _call(mcp, "add_junction", x_mm=101.6, y_mm=127.0)
    tree = sch_io.parse_file(blank_project["sch"])
    (j,) = sch_io.find_children(tree, "junction")
    at = sch_io.find_child(j, "at")
    assert (at[1], at[2]) == (101.6, round(208.28 - 127.0, 4))
    assert sch_io.find_child(j, "uuid") is not None


def test_set_symbol_field_tool(blank_project, tmp_path, monkeypatch):
    mcp = _make_mcp_with_fixture_index(monkeypatch, tmp_path)
    _call(mcp, "add_symbol", lib_id="MiniLib:Resistor", reference="R1", value="10k", x_mm=101.6, y_mm=101.6)
    _call(mcp, "add_symbol", lib_id="MiniLib:Resistor", reference="R2", value="1k", x_mm=127, y_mm=101.6)

    res = _call(mcp, "set_symbol_field", reference="R1", field="Footprint",
                value="Resistor_SMD:R_0805_2012Metric")
    assert res["created"] is False
    res = _call(mcp, "set_symbol_field", reference="R1", field="MPN", value="RC0805FR-0710KL")
    assert res["created"] is True

    tree = sch_io.parse_file(blank_project["sch"])
    s = ed.find_symbol_by_reference(tree, "R1")
    assert ed.get_symbol_property(s, "Footprint") == "Resistor_SMD:R_0805_2012Metric"
    assert ed.get_symbol_property(s, "MPN") == "RC0805FR-0710KL"
    assert ed._is_hidden(ed._find_property(s, "MPN"))

    _call(mcp, "set_symbol_field", reference="R1", field="MPN", value="X", visible=True)
    tree = sch_io.parse_file(blank_project["sch"])
    assert not ed._is_hidden(ed._find_property(ed.find_symbol_by_reference(tree, "R1"), "MPN"))

    # Renaming the reference updates the instance path too; duplicates are refused.
    with pytest.raises(ValueError, match="already exists"):
        _call(mcp, "set_symbol_field", reference="R1", field="Reference", value="R2")
    _call(mcp, "set_symbol_field", reference="R1", field="Reference", value="R10")
    tree = sch_io.parse_file(blank_project["sch"])
    s = ed.find_symbol_by_reference(tree, "R10")
    inst_ref = sch_io.find_child(
        sch_io.find_child(sch_io.find_child(sch_io.find_child(s, "instances"), "project"), "path"),
        "reference",
    )
    assert inst_ref[1] == "R10"


def test_set_symbol_field_unknown_reference(blank_project, tmp_path, monkeypatch):
    mcp = _make_mcp_with_fixture_index(monkeypatch, tmp_path)
    with pytest.raises(KeyError):
        _call(mcp, "set_symbol_field", reference="R9", field="Value", value="1k")


# ===== ERC on a real, on-grid circuit (slow) =============================== #


@pytest.mark.slow
def test_on_grid_circuit_has_no_off_grid_erc_warnings(tmp_path):
    """Build a divider on the 2.54 grid; ERC must report no endpoint_off_grid."""
    from kicad_claude.tools import validation as val_tools

    if not find_kicad_cli() or not find_symbol_lib_dirs():
        pytest.skip("no kicad-cli or KiCAD symbol libs available")
    cached = kicad_libs.load_cache()
    if cached is None:
        pytest.skip("library index not built; run `index_libraries` first")
    state.clear_active()
    lib_tools._index = cached
    write_blank_project(tmp_path / "grid", "grid")
    state.set_active(tmp_path / "grid", "grid")

    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP("test")
    sch_tools.register(mcp)
    lib_tools.register(mcp)
    val_tools.register(mcp)

    try:
        _call(mcp, "add_power_symbol", net="+5V", x_mm=101.6, y_mm=152.4)
        _call(mcp, "add_symbol", lib_id="Device:R", reference="R1", value="10k", x_mm=101.6, y_mm=127)
        _call(mcp, "add_symbol", lib_id="Device:R", reference="R2", value="1k", x_mm=101.6, y_mm=101.6)
        _call(mcp, "add_symbol", lib_id="Device:C", reference="C1", value="100n",
              x_mm=127, y_mm=114.3, rotation=90)
        _call(mcp, "add_power_symbol", net="GND", x_mm=101.6, y_mm=76.2)

        def pin(ref, num):
            return _call(mcp, "get_pin_position", reference=ref, pin=num)["position_mm"]

        def wire(a, b):
            _call(mcp, "add_wire", x1_mm=a[0], y1_mm=a[1], x2_mm=b[0], y2_mm=b[1])

        wire([101.6, 152.4], pin("R1", "1"))
        wire(pin("R1", "2"), pin("R2", "1"))
        wire(pin("R2", "2"), [101.6, 76.2])
        mid = [101.6, 114.3]
        wire(mid, pin("C1", "1"))
        _call(mcp, "add_junction", x_mm=mid[0], y_mm=mid[1])
        # C1 pin 2 left open on purpose: ERC must surface it as an error.

        erc = _call(mcp, "run_erc")
    finally:
        state.clear_active()

    assert "endpoint_off_grid" not in erc["by_type"], erc["by_type"]
    assert erc["by_type"].get("pin_not_connected", 0) >= 1
    assert erc["errors"] >= 1
    assert erc["total_violations"] == erc["errors"] + erc["warnings"] + erc["exclusions"]


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
