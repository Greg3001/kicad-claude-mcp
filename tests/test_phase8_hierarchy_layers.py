"""Phase 8 — hierarchical schematics + multi-layer PCBs.

Strategy:
- Unit tests for the layer-name and stackup builders.
- Unit tests for sheet operations on synthetic trees.
- @pytest.mark.slow: real kicad-cli runs against:
    1) a 12-layer board with inner-layer tracks
    2) a 2-level hierarchical schematic
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from kicad_claude import state
from kicad_claude.adapters import pcb_editor as pcb_ed
from kicad_claude.adapters import pcb_layers, sch_editor as sch_ed, sch_io
from kicad_claude.indexer import kicad_libs
from kicad_claude.templates.blank import write_blank_project, write_blank_schematic
from kicad_claude.tools import library as lib_tools
from kicad_claude.tools import pcb as pcb_tools
from kicad_claude.tools import schematic as sch_tools
from kicad_claude.utils.kicad_paths import find_kicad_cli, find_symbol_lib_dirs

FIXTURES = Path(__file__).parent / "fixtures"


# ===== Layer builders ====================================================== #


def test_copper_layer_names_for_supported_counts():
    assert pcb_layers.copper_layer_names(2) == ["F.Cu", "B.Cu"]
    assert pcb_layers.copper_layer_names(4) == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
    assert len(pcb_layers.copper_layer_names(12)) == 12
    assert pcb_layers.copper_layer_names(12)[0] == "F.Cu"
    assert pcb_layers.copper_layer_names(12)[-1] == "B.Cu"
    assert pcb_layers.copper_layer_names(12)[1:-1] == [f"In{k}.Cu" for k in range(1, 11)]


def test_copper_layer_id_matches_kicad_pattern():
    # Empirically verified against pcbnew's SetCopperLayerCount output
    assert pcb_layers.copper_layer_id("F.Cu") == 0
    assert pcb_layers.copper_layer_id("B.Cu") == 2
    assert pcb_layers.copper_layer_id("In1.Cu") == 4
    assert pcb_layers.copper_layer_id("In2.Cu") == 6
    assert pcb_layers.copper_layer_id("In10.Cu") == 22


def test_copper_layer_count_must_be_even_and_in_range():
    with pytest.raises(ValueError):
        pcb_layers.copper_layer_names(3)
    with pytest.raises(ValueError):
        pcb_layers.copper_layer_names(0)
    with pytest.raises(ValueError):
        pcb_layers.copper_layer_names(34)


def test_build_layers_block_has_signal_and_user_layers():
    block = pcb_layers.build_layers_block(4)
    # Skip header symbol; should have 4 signal rows + 18 user rows.
    rows = block[1:]
    signal_rows = [r for r in rows if str(r[2]) == "signal"]
    user_rows = [r for r in rows if str(r[2]) == "user"]
    assert len(signal_rows) == 4
    assert len(user_rows) == 18


def test_build_stackup_total_thickness_near_target():
    """Sanity: dielectric thickness chosen so total ≈ TARGET_BOARD_THICKNESS_MM."""
    for n in (2, 4, 6, 12):
        diel_t = pcb_layers._dielectric_thickness(n)
        total = n * pcb_layers.COPPER_THICKNESS_MM + (n - 1) * diel_t
        assert abs(total - pcb_layers.TARGET_BOARD_THICKNESS_MM) < 0.01


# ===== set_copper_layer_count on a tree ==================================== #


@pytest.fixture
def blank_project(tmp_path: Path):
    state.clear_active()
    files = write_blank_project(tmp_path / "p", "p")
    state.set_active(tmp_path / "p", "p")
    yield files
    state.clear_active()


def test_set_copper_layer_count_round_trip(blank_project):
    tree = sch_io.parse_file(blank_project["pcb"])
    assert pcb_ed.get_copper_layer_count(tree) == 2

    pcb_ed.set_copper_layer_count(tree, 6)
    assert pcb_ed.get_copper_layer_count(tree) == 6
    names = pcb_ed.get_copper_layer_names(tree)
    assert names == ["F.Cu", "In1.Cu", "In2.Cu", "In3.Cu", "In4.Cu", "B.Cu"]

    # Bring back down to 2; user layers must remain intact.
    pcb_ed.set_copper_layer_count(tree, 2)
    assert pcb_ed.get_copper_layer_count(tree) == 2


def test_set_copper_layer_count_rejects_invalid(blank_project):
    tree = sch_io.parse_file(blank_project["pcb"])
    with pytest.raises(ValueError):
        pcb_ed.set_copper_layer_count(tree, 3)
    with pytest.raises(ValueError):
        pcb_ed.set_copper_layer_count(tree, 0)


# ===== set_layer_count tool ================================================ #


def test_set_layer_count_tool(blank_project, monkeypatch):
    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP("t")
    pcb_tools.register(mcp)

    def call(name, **kw):
        return mcp._tool_manager.get_tool(name).fn(**kw)

    res = call("set_layer_count", n=8)
    assert res["copper_layers"] == 8
    assert res["layer_names"] == ["F.Cu", *[f"In{k}.Cu" for k in range(1, 7)], "B.Cu"]

    tree = sch_io.parse_file(blank_project["pcb"])
    assert pcb_ed.get_copper_layer_count(tree) == 8


# ===== Hierarchical sheet helpers (unit) =================================== #


def test_add_sheet_node_appends_and_blocks_duplicates(blank_project):
    tree = sch_io.parse_file(blank_project["sch"])
    sch_ed.add_sheet_node(
        tree, sheet_name="PSU", sheet_filename="psu.kicad_sch",
        x_mm=50, y_mm=50, width_mm=30, height_mm=20, project_name="p",
    )
    sheets = sch_ed.list_sheets(tree)
    assert len(sheets) == 1
    assert sheets[0]["name"] == "PSU"
    # Adding same filename again raises
    with pytest.raises(ValueError):
        sch_ed.add_sheet_node(
            tree, sheet_name="OtherName", sheet_filename="psu.kicad_sch",
            x_mm=20, y_mm=20, width_mm=10, height_mm=10, project_name="p",
        )


def test_add_hierarchical_label_validates_shape(blank_project):
    tree = sch_io.parse_file(blank_project["sch"])
    sch_ed.add_hierarchical_label(
        tree, net_name="+5V", x_mm=50, y_mm=50, shape="input",
    )
    labels = sch_io.find_children(tree, "hierarchical_label")
    assert len(labels) == 1
    with pytest.raises(ValueError):
        sch_ed.add_hierarchical_label(
            tree, net_name="X", x_mm=0, y_mm=0, shape="bogus",
        )


# ===== Hierarchy via the MCP tool layer ==================================== #


def _patched_index_with_minilib(tmp_path):
    sym_dir = tmp_path / "syms"
    sym_dir.mkdir()
    shutil.copy(FIXTURES / "MiniLib.kicad_sym", sym_dir / "MiniLib.kicad_sym")
    return kicad_libs.build_index(symbol_dirs=[sym_dir], footprint_dirs=[])


def _make_mcp(monkeypatch, idx):
    from mcp.server.fastmcp import FastMCP
    monkeypatch.setattr(lib_tools, "load_cache", lambda: idx)
    monkeypatch.setattr(lib_tools, "_index", None)
    mcp = FastMCP("t")
    sch_tools.register(mcp)
    lib_tools.register(mcp)
    return mcp


def _call(mcp, name, **kw):
    return mcp._tool_manager.get_tool(name).fn(**kw)


def test_hierarchy_round_trip(blank_project, tmp_path, monkeypatch):
    idx = _patched_index_with_minilib(tmp_path)
    mcp = _make_mcp(monkeypatch, idx)

    # Initially on root
    assert _call(mcp, "get_active_sheet")["active_sheet"] == "root"

    # Create a child and switch to it
    res = _call(mcp, "add_sheet", sheet_name="Power", x_mm=70, y_mm=120)
    assert Path(res["child_path"]).is_file()

    sheets = _call(mcp, "list_sheets")
    assert sheets["count"] == 1
    assert sheets["children"][0]["name"] == "Power"

    _call(mcp, "set_active_sheet", sheet="Power")
    assert _call(mcp, "get_active_sheet")["active_sheet"] == "power.kicad_sch"

    # Add a symbol in the child — instance path must reference root_uuid + sheet_uuid
    _call(mcp, "add_symbol",
          lib_id="MiniLib:Resistor", reference="R1", value="10k",
          x_mm=80, y_mm=80)
    _call(mcp, "add_hierarchical_label",
          net_name="+5V", x_mm=60, y_mm=80, shape="input")

    # Back to root + add a sheet pin
    _call(mcp, "set_active_sheet", sheet="root")
    _call(mcp, "add_sheet_pin",
          sheet_name="Power", pin_name="+5V", shape="input",
          x_mm=70, y_mm=130)

    # Verify symbol instance path in the child file uses /<root>/<sheet>
    proj = state.get_active()
    child_tree = sch_io.parse_file(proj.path / "power.kicad_sch")
    r1 = sch_ed.find_symbol_by_reference(child_tree, "R1")
    assert r1 is not None
    instances = sch_io.find_child(r1, "instances")
    project_node = sch_io.find_child(instances, "project")
    path_node = sch_io.find_child(project_node, "path")
    instance_path = path_node[1]
    # Should be "/<root_uuid>/<sheet_uuid>" — two segments after the leading '/'
    parts = instance_path.strip("/").split("/")
    assert len(parts) == 2

    # Root has a (sheet ...) with one (pin "+5V" ...)
    root_tree = sch_io.parse_file(proj.sch_path)
    sheet_node = sch_ed.find_sheet_by_name(root_tree, "Power")
    pins = [c for c in sheet_node[1:] if sch_io.is_call(c, "pin")]
    assert any(p[1] == "+5V" for p in pins)


def test_set_active_sheet_root_returns_to_proj_sch(blank_project, tmp_path, monkeypatch):
    idx = _patched_index_with_minilib(tmp_path)
    mcp = _make_mcp(monkeypatch, idx)
    _call(mcp, "add_sheet", sheet_name="X")
    _call(mcp, "set_active_sheet", sheet="X")
    _call(mcp, "set_active_sheet", sheet="")  # back to root
    assert _call(mcp, "get_active_sheet")["active_sheet"] == "root"


def test_hierarchical_label_refused_on_root(blank_project, tmp_path, monkeypatch):
    idx = _patched_index_with_minilib(tmp_path)
    mcp = _make_mcp(monkeypatch, idx)
    with pytest.raises(RuntimeError, match="sub-sheet"):
        _call(mcp, "add_hierarchical_label", net_name="X", x_mm=10, y_mm=10)


# ===== Acceptance: kicad-cli erc on a hierarchical project ================= #


@pytest.mark.slow
def test_acceptance_hierarchy_kicad_cli(tmp_path):
    cli = find_kicad_cli()
    if not cli or not find_symbol_lib_dirs():
        pytest.skip("kicad-cli or KiCAD libs not available")
    cached = kicad_libs.load_cache()
    if cached is None:
        pytest.skip("library index not built")

    state.clear_active()
    lib_tools._index = cached

    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP("t")
    sch_tools.register(mcp)
    lib_tools.register(mcp)

    files = write_blank_project(tmp_path / "h", "h")
    state.set_active(tmp_path / "h", "h")

    def call(name, **kw):
        return mcp._tool_manager.get_tool(name).fn(**kw)

    # Root: power supply on a sub-sheet, peripherals on another
    call("add_sheet", sheet_name="PSU", x_mm=50, y_mm=140)
    call("add_sheet", sheet_name="Periph", x_mm=110, y_mm=140)

    # Inside PSU: +5V → R1 → GND
    call("set_active_sheet", sheet="PSU")
    call("add_power_symbol", net="+5V", x_mm=60, y_mm=120)
    call("add_symbol", lib_id="Device:R", reference="R1", value="10k",
         x_mm=60, y_mm=90)
    call("add_power_symbol", net="GND", x_mm=60, y_mm=70)

    # Inside Periph: another resistor
    call("set_active_sheet", sheet="Periph")
    call("add_symbol", lib_id="Device:R", reference="R2", value="1k",
         x_mm=60, y_mm=90)

    # Back to root
    call("set_active_sheet", sheet="")

    # kicad-cli must parse the hierarchy
    r = subprocess.run(
        [str(cli), "sch", "erc", "--format", "json", str(files["sch"])],
        capture_output=True, text=True, timeout=60, cwd=tmp_path,
    )
    assert r.returncode == 0, f"erc failed: stderr={r.stderr[-300:]}"
    state.clear_active()


@pytest.mark.slow
def test_acceptance_12_layer_pcb_drc_clean(tmp_path):
    cli = find_kicad_cli()
    if not cli or not find_symbol_lib_dirs():
        pytest.skip("kicad-cli or KiCAD libs not available")
    cached = kicad_libs.load_cache()
    if cached is None:
        pytest.skip("library index not built")

    state.clear_active()
    lib_tools._index = cached

    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP("t")
    pcb_tools.register(mcp)

    files = write_blank_project(tmp_path / "deep", "deep")
    state.set_active(tmp_path / "deep", "deep")

    def call(name, **kw):
        return mcp._tool_manager.get_tool(name).fn(**kw)

    call("set_layer_count", n=12)
    call("set_board_outline", width_mm=80, height_mm=60)
    # Add tracks scattered across copper layers
    call("add_track", x1_mm=20, y1_mm=20, x2_mm=60, y2_mm=20, layer="F.Cu")
    call("add_track", x1_mm=20, y1_mm=25, x2_mm=60, y2_mm=25, layer="In3.Cu")
    call("add_track", x1_mm=20, y1_mm=30, x2_mm=60, y2_mm=30, layer="In7.Cu")
    call("add_track", x1_mm=20, y1_mm=35, x2_mm=60, y2_mm=35, layer="B.Cu")

    r = subprocess.run(
        [str(cli), "pcb", "drc", "--format", "json", "-o",
         str(tmp_path / "drc.json"), str(files["pcb"])],
        capture_output=True, text=True, timeout=60, cwd=tmp_path,
    )
    assert r.returncode == 0, f"drc parse failed: stderr={r.stderr[-300:]}"

    drc = json.loads((tmp_path / "drc.json").read_text())
    # No tracks-on-undefined-layer errors should appear (would mean layers
    # block doesn't actually contain those copper layers).
    bad = [
        v for v in drc.get("violations", [])
        if "layer" in (v.get("description") or "").lower()
        and "undefined" in (v.get("description") or "").lower()
    ]
    assert not bad, f"unexpected layer-related violations: {bad}"
    state.clear_active()
