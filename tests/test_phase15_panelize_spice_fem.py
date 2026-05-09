"""Phase 15 — panelization, SPICE wrapper, thermal/EMC closed-form FEM.

Pure-Python tests for the math + adapter logic. Slow tests verify the
panel survives kicad-cli pcb drc.
"""

from __future__ import annotations

import math
import shutil
from pathlib import Path

import pytest
import sexpdata

from kicad_claude import state
from kicad_claude.adapters import (
    pcb_editor as pcb_ed, panelization, sch_io, thermal_emc,
)
from kicad_claude.adapters.sch_io import sym
from kicad_claude.indexer import kicad_libs
from kicad_claude.templates.blank import write_blank_project
from kicad_claude.tools import library as lib_tools
from kicad_claude.utils.kicad_paths import find_kicad_cli


# ===== Panelization adapter ================================================ #


def test_translate_segment_moves_endpoints():
    seg = [
        sym("segment"),
        [sym("start"), 10.0, 20.0],
        [sym("end"), 30.0, 20.0],
        [sym("width"), 0.25],
        [sym("layer"), "F.Cu"],
        [sym("net"), 0],
        [sym("uuid"), "u1"],
    ]
    new = panelization.translate_element(seg, dx_kicad=5.0, dy_kicad=10.0)
    start = sch_io.find_child(new, "start")
    end = sch_io.find_child(new, "end")
    assert start[1] == 15.0 and start[2] == 30.0
    assert end[1] == 35.0 and end[2] == 30.0
    # UUID should be refreshed
    new_uuid = sch_io.find_child(new, "uuid")[1]
    assert new_uuid != "u1"


def test_translate_via_moves_at():
    via = [
        sym("via"),
        [sym("at"), 50.0, 50.0],
        [sym("size"), 0.6],
        [sym("drill"), 0.3],
        [sym("layers"), "F.Cu", "B.Cu"],
        [sym("net"), 0],
        [sym("uuid"), "v1"],
    ]
    new = panelization.translate_element(via, dx_kicad=10, dy_kicad=20)
    at = sch_io.find_child(new, "at")
    assert at[1] == 60.0 and at[2] == 70.0


def test_translate_zone_moves_polygon_points():
    zone = [
        sym("zone"),
        [sym("net"), 0],
        [sym("net_name"), "GND"],
        [sym("layer"), "F.Cu"],
        [sym("uuid"), "z1"],
        [sym("polygon"),
         [sym("pts"),
          [sym("xy"), 0.0, 0.0],
          [sym("xy"), 10.0, 0.0],
          [sym("xy"), 10.0, 10.0],
          [sym("xy"), 0.0, 10.0]]],
    ]
    new = panelization.translate_element(zone, dx_kicad=5, dy_kicad=5)
    pts = sch_io.find_child(sch_io.find_child(new, "polygon"), "pts")
    coords = [(c[1], c[2]) for c in pts[1:] if sch_io.is_call(c, "xy")]
    assert (5, 5) in coords
    assert (15, 5) in coords
    assert (15, 15) in coords


def test_panelize_grid_full_run(tmp_path: Path):
    """Build a tiny source PCB, panelize 2x2, verify cell count + outline."""
    state.clear_active()
    files = write_blank_project(tmp_path / "p", "p")
    state.set_active(tmp_path / "p", "p")

    tree = sch_io.parse_file(files["pcb"])
    pcb_ed.set_board_outline(tree, 30, 20)
    sch_io.write_file(files["pcb"], tree)

    src = sch_io.parse_file(files["pcb"])
    res = panelization.panelize_grid(
        src, rows=2, cols=2, h_gap_mm=1.0, v_gap_mm=1.0, mouse_bites=False,
    )
    assert res["cell_count"] == 4
    # Panel outline = 2*30 + 1 = 61 wide, 2*20 + 1 = 41 tall
    assert res["outline_size_mm"] == [61.0, 41.0]
    state.clear_active()


def test_panelize_rejects_no_outline(tmp_path: Path):
    state.clear_active()
    files = write_blank_project(tmp_path / "q", "q")
    state.set_active(tmp_path / "q", "q")
    src = sch_io.parse_file(files["pcb"])  # no outline
    with pytest.raises(RuntimeError, match="rectangular"):
        panelization.panelize_grid(src, rows=2, cols=2)
    state.clear_active()


def test_panelize_rejects_bad_grid(tmp_path: Path):
    state.clear_active()
    files = write_blank_project(tmp_path / "r", "r")
    state.set_active(tmp_path / "r", "r")
    tree = sch_io.parse_file(files["pcb"])
    pcb_ed.set_board_outline(tree, 20, 20)
    sch_io.write_file(files["pcb"], tree)
    src = sch_io.parse_file(files["pcb"])
    with pytest.raises(ValueError):
        panelization.panelize_grid(src, rows=0, cols=2)
    with pytest.raises(ValueError):
        panelization.panelize_grid(src, rows=1, cols=-1)
    state.clear_active()


def test_panelize_includes_mouse_bites(tmp_path: Path):
    state.clear_active()
    files = write_blank_project(tmp_path / "s", "s")
    state.set_active(tmp_path / "s", "s")
    tree = sch_io.parse_file(files["pcb"])
    pcb_ed.set_board_outline(tree, 30, 20)
    sch_io.write_file(files["pcb"], tree)
    src = sch_io.parse_file(files["pcb"])
    res = panelization.panelize_grid(
        src, rows=1, cols=2, h_gap_mm=2.0, v_gap_mm=2.0,
        mouse_bites=True, mouse_bite_spacing_mm=2.0,
    )
    panel = res["tree"]
    # gr_circle nodes on Edge.Cuts are mouse bites
    edge_cuts_circles = [
        c for c in sch_io.find_children(panel, "gr_circle")
        if (sch_io.find_child(c, "layer") or [None, ""])[1] == "Edge.Cuts"
    ]
    assert len(edge_cuts_circles) >= 5  # at least 5 holes between 2 cells
    state.clear_active()


# ===== Thermal network ===================================================== #


def test_thermal_simple_one_component():
    res = thermal_emc.solve_thermal_network(
        [{"reference": "U1", "power_w": 1.0, "r_jc_c_per_w": 1.5, "r_ca_c_per_w": 50}],
        ambient_c=25,
    )
    assert len(res) == 1
    expected_t = 25 + 1.0 * (1.5 + 50)
    assert math.isclose(res[0]["junction_temp_c"], expected_t, abs_tol=0.01)


def test_thermal_sorts_hottest_first():
    res = thermal_emc.solve_thermal_network(
        [
            {"reference": "Q1", "power_w": 0.1},
            {"reference": "U1", "power_w": 5.0},
            {"reference": "C1", "power_w": 0.0},
        ],
    )
    assert res[0]["reference"] == "U1"
    assert res[-1]["reference"] == "C1"


def test_thermal_warning_thresholds():
    res = thermal_emc.solve_thermal_network([
        {"reference": "OK", "power_w": 0.5, "r_jc_c_per_w": 1, "r_ca_c_per_w": 50},
        {"reference": "WARN", "power_w": 1.5, "r_jc_c_per_w": 1, "r_ca_c_per_w": 50},
        {"reference": "BAD", "power_w": 3.0, "r_jc_c_per_w": 1, "r_ca_c_per_w": 50},
    ])
    by_ref = {r["reference"]: r for r in res}
    assert by_ref["OK"]["warning"] == ""
    assert "approaching" in by_ref["WARN"]["warning"]
    assert "exceeds" in by_ref["BAD"]["warning"]


# ===== Crosstalk =========================================================== #


def test_crosstalk_decreases_with_separation():
    near = thermal_emc.estimate_crosstalk_coupling(
        parallel_length_mm=20, separation_mm=0.2,
        dielectric_height_mm=0.21, rise_time_ns=1.0,
    )
    far = thermal_emc.estimate_crosstalk_coupling(
        parallel_length_mm=20, separation_mm=2.0,
        dielectric_height_mm=0.21, rise_time_ns=1.0,
    )
    assert near["coupling_factor"] > far["coupling_factor"]
    assert near["near_end_crosstalk_ratio"] > far["near_end_crosstalk_ratio"]


def test_crosstalk_increases_with_length():
    short = thermal_emc.estimate_crosstalk_coupling(
        parallel_length_mm=5, separation_mm=0.3,
        dielectric_height_mm=0.21, rise_time_ns=1.0,
    )
    long = thermal_emc.estimate_crosstalk_coupling(
        parallel_length_mm=50, separation_mm=0.3,
        dielectric_height_mm=0.21, rise_time_ns=1.0,
    )
    assert long["far_end_crosstalk_ratio"] > short["far_end_crosstalk_ratio"]


def test_crosstalk_rejects_invalid():
    with pytest.raises(ValueError):
        thermal_emc.estimate_crosstalk_coupling(
            parallel_length_mm=0, separation_mm=0.3,
            dielectric_height_mm=0.21, rise_time_ns=1.0,
        )
    with pytest.raises(ValueError):
        thermal_emc.estimate_crosstalk_coupling(
            parallel_length_mm=10, separation_mm=-0.1,
            dielectric_height_mm=0.21, rise_time_ns=1.0,
        )


# ===== Return path heuristic =============================================== #


def test_return_path_full_coverage():
    """Track sitting in the middle of a GND zone gets 100% coverage."""
    seg = [{
        "net": "DATA", "layer": "F.Cu",
        "start": (15.0, 100.0), "end": (25.0, 100.0),
    }]
    zone = [{
        "layer": "B.Cu",
        "polygon": [(0, 80), (40, 80), (40, 120), (0, 120)],
    }]
    findings = thermal_emc.check_return_path(seg, zone)
    assert findings == []  # fully covered, no issues


def test_return_path_no_zone_flagged():
    seg = [{"net": "DATA", "layer": "F.Cu", "start": (0, 0), "end": (10, 0)}]
    findings = thermal_emc.check_return_path(seg, [])
    assert len(findings) == 1
    assert "no GND reference" in findings[0]["issue"]


def test_return_path_partial_coverage_flagged():
    """Track halfway outside a zone gets <80% coverage and is flagged."""
    seg = [{
        "net": "DATA", "layer": "F.Cu",
        "start": (0.0, 0.0), "end": (20.0, 0.0),  # 20 mm long
    }]
    zone = [{
        "layer": "B.Cu",
        "polygon": [(0, -5), (10, -5), (10, 5), (0, 5)],  # only first 10 mm
    }]
    findings = thermal_emc.check_return_path(seg, zone)
    assert len(findings) == 1
    assert findings[0]["coverage_pct"] < 80


# ===== Tool layer ========================================================== #


def _make_panelize_mcp(tmp_path):
    cached = kicad_libs.load_cache()
    if cached is None:
        pytest.skip("library index not built")
    state.clear_active()
    lib_tools._index = cached
    files = write_blank_project(tmp_path / "src", "src")
    state.set_active(tmp_path / "src", "src")

    from mcp.server.fastmcp import FastMCP
    from kicad_claude.tools import (
        library, pcb as pcb_tools, panelization as pan_tools,
    )
    mcp = FastMCP("t")
    for mod in (library, pcb_tools, pan_tools):
        mod.register(mcp)
    return mcp, files


def _call(mcp, _name, **kw):
    return mcp._tool_manager.get_tool(_name).fn(**kw)


def test_panelize_tool_writes_panel(tmp_path: Path):
    mcp, files = _make_panelize_mcp(tmp_path)
    _call(mcp, "set_board_outline", width_mm=30, height_mm=20)
    _call(mcp, "add_footprint", lib_id="Resistor_SMD:R_0603_1608Metric",
          reference="R1", value="10k", x_mm=15, y_mm=10)

    res = _call(mcp, "panelize_board_grid", rows=2, cols=2)
    panel_path = Path(res["panel_path"])
    assert panel_path.is_file()
    assert res["cell_count"] == 4
    # Verify the panel file is parseable
    panel_tree = sch_io.parse_file(panel_path)
    fps = sch_io.find_children(panel_tree, "footprint")
    # 4 cells × 1 footprint = 4 footprints
    assert len(fps) == 4
    # References suffixed
    refs = []
    for fp in fps:
        for prop in sch_io.find_children(fp, "property"):
            if len(prop) >= 3 and prop[1] == "Reference":
                refs.append(prop[2])
    assert sorted(refs) == ["R1_R1C1", "R1_R1C2", "R1_R2C1", "R1_R2C2"]
    state.clear_active()


def test_simulation_tools_thermal_and_crosstalk(tmp_path: Path):
    mcp, files = _make_panelize_mcp(tmp_path)
    from kicad_claude.tools import simulation as sim_tools
    sim_tools.register(mcp)

    res = _call(mcp, "simulate_thermal_steady_state",
                components=[
                    {"reference": "U1", "power_w": 2, "r_jc_c_per_w": 2, "r_ca_c_per_w": 50},
                    {"reference": "Q1", "power_w": 0.5, "r_jc_c_per_w": 1.5, "r_ca_c_per_w": 60},
                ],
                ambient_c=25)
    assert res["component_count"] == 2
    assert res["max_junction_temp_c"] > 25
    # Hottest first
    assert res["components"][0]["reference"] == "U1"

    res = _call(mcp, "estimate_crosstalk",
                parallel_length_mm=15, separation_mm=0.3,
                dielectric_height_mm=0.21, rise_time_ns=1.0)
    assert "near_end_crosstalk_ratio" in res
    assert 0 < res["coupling_factor"] <= 1
    state.clear_active()


# ===== Slow acceptance ==================================================== #


@pytest.mark.slow
def test_acceptance_panel_kicad_cli_parses(tmp_path: Path):
    """The generated panel file must be readable by kicad-cli."""
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
        library, pcb as pcb_tools, panelization as pan_tools,
    )
    mcp = FastMCP("t")
    for mod in (library, pcb_tools, pan_tools):
        mod.register(mcp)
    def call(name, **kw):
        return mcp._tool_manager.get_tool(name).fn(**kw)

    call("set_board_outline", width_mm=30, height_mm=20)
    call("add_footprint",
         lib_id="Resistor_SMD:R_0603_1608Metric",
         reference="R1", value="10k", x_mm=15, y_mm=10)
    call("add_track", x1_mm=15, y1_mm=10, x2_mm=20, y2_mm=10)

    res = call("panelize_board_grid", rows=2, cols=3,
               h_gap_mm=2, v_gap_mm=2, mouse_bites=True)

    import subprocess
    cli = find_kicad_cli()
    r = subprocess.run(
        [str(cli), "pcb", "drc", "--format", "json", res["panel_path"]],
        capture_output=True, text=True, timeout=60, cwd=tmp_path,
    )
    assert r.returncode == 0
    state.clear_active()


@pytest.mark.slow
def test_acceptance_spice_netlist_export(tmp_path: Path):
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
    from kicad_claude.tools import library, spice as spice_tools
    mcp = FastMCP("t")
    for mod in (library, spice_tools):
        mod.register(mcp)
    res = mcp._tool_manager.get_tool("export_spice_netlist").fn()
    assert Path(res["netlist_path"]).is_file()
    assert res["size_bytes"] > 0
    state.clear_active()
