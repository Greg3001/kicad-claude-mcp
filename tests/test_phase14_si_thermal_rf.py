"""Phase 14 — signal integrity, thermal, RF, EMC.

Pure-Python tests on the formulas (no kicad-cli) plus end-to-end smoke
tests on the tool layer.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest
import sexpdata

from kicad_claude import state
from kicad_claude.adapters import electrical_calc as ec
from kicad_claude.adapters import pcb_editor as pcb_ed
from kicad_claude.adapters import sch_io
from kicad_claude.indexer import kicad_libs
from kicad_claude.templates.blank import write_blank_project
from kicad_claude.tools import library as lib_tools


# ===== electrical_calc — formulas ========================================== #


def test_microstrip_50ohm_typical_4layer():
    """For h=0.21mm FR4 (typical 4-layer prepreg), 50Ω → ~0.34mm."""
    z = ec.microstrip_impedance(0.34, 0.21, er=4.5, thickness_mm=0.035)
    assert 49.0 < z < 51.0


def test_microstrip_2layer_typical():
    """h=1.5mm 2-layer FR4 needs ~2.8mm width for 50Ω."""
    w50 = ec.solve_microstrip_width(50, 1.5, er=4.5)
    assert 2.5 < w50 < 3.0
    # Verify
    z = ec.microstrip_impedance(w50, 1.5, er=4.5)
    assert abs(z - 50) < 1.0


def test_stripline_inner_layer():
    """Inner-layer stripline has lower Z₀ than microstrip for same w (more copper coupling)."""
    z_micro = ec.microstrip_impedance(0.5, 0.5, er=4.5)
    z_strip = ec.stripline_impedance(0.5, 1.0, er=4.5)  # b=2h
    assert z_strip < z_micro


def test_differential_microstrip_lower_than_2x_single():
    """Differential coupling reduces Z below 2× single-ended."""
    w, h, gap = 0.2, 0.21, 0.2
    z_diff = ec.differential_microstrip_impedance(w, gap, h)
    z0 = ec.microstrip_impedance(w, h)
    assert z_diff < 2 * z0


def test_solve_microstrip_width_round_trip():
    """Solving for width then computing Z₀ should round-trip within tolerance."""
    target = 50
    w = ec.solve_microstrip_width(target, 0.21, er=4.5)
    z = ec.microstrip_impedance(w, 0.21, er=4.5)
    assert abs(z - target) < 0.5


def test_solve_microstrip_unreachable_raises():
    with pytest.raises(ValueError, match="outside achievable"):
        ec.solve_microstrip_width(500, 0.21)  # 500Ω needs ridiculous width


def test_microstrip_rejects_invalid_inputs():
    with pytest.raises(ValueError):
        ec.microstrip_impedance(-0.1, 0.21)
    with pytest.raises(ValueError):
        ec.microstrip_impedance(0.4, 0)
    with pytest.raises(ValueError):
        ec.microstrip_impedance(0.4, 0.21, er=0)


def test_trace_current_ipc2152_typical():
    """0.5mm 1oz external @ ΔT=10°C → ~1.4 A."""
    amps = ec.trace_current_ipc2152(0.5, copper_thickness_mm=0.035, temp_rise_c=10, location="external")
    assert 1.2 < amps < 1.6


def test_trace_current_internal_lower_than_external():
    """Internal layers carry less current (worse cooling)."""
    ext = ec.trace_current_ipc2152(0.5, location="external")
    intl = ec.trace_current_ipc2152(0.5, location="internal")
    assert intl < ext


def test_solve_trace_width_for_current_round_trip():
    target = 2.0
    w = ec.solve_trace_width_for_current(target, copper_thickness_mm=0.035, temp_rise_c=10)
    actual = ec.trace_current_ipc2152(w, copper_thickness_mm=0.035, temp_rise_c=10)
    assert abs(actual - target) < 0.01


def test_trace_current_rejects_invalid():
    with pytest.raises(ValueError):
        ec.trace_current_ipc2152(-0.5)
    with pytest.raises(ValueError):
        ec.solve_trace_width_for_current(0)


# ===== Tool layer ========================================================== #


@pytest.fixture
def board_with_tracks(tmp_path: Path):
    cached = kicad_libs.load_cache()
    if cached is None:
        pytest.skip("library index not built")
    state.clear_active()
    lib_tools._index = cached
    files = write_blank_project(tmp_path / "p", "p")
    state.set_active(tmp_path / "p", "p")

    # Set 4 layers + outline
    tree = sch_io.parse_file(files["pcb"])
    pcb_ed.set_copper_layer_count(tree, 4)
    pcb_ed.set_board_outline(tree, 80, 60)
    # Add a few tracks of varying widths and layers
    pcb_ed.add_track(tree, 10, 10, 70, 10, width_mm=0.25, layer="F.Cu")
    pcb_ed.add_track(tree, 10, 20, 70, 20, width_mm=0.5, layer="F.Cu")
    pcb_ed.add_track(tree, 10, 30, 70, 30, width_mm=1.0, layer="In1.Cu")
    sch_io.write_file(files["pcb"], tree)
    yield files
    state.clear_active()


def _make_mcp(modules):
    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP("t")
    for mod in modules:
        mod.register(mcp)
    return mcp


def _call(mcp, _name, **kw):
    return mcp._tool_manager.get_tool(_name).fn(**kw)


def test_signal_integrity_tools(board_with_tracks):
    from kicad_claude.tools import signal_integrity
    mcp = _make_mcp([signal_integrity])
    res = _call(mcp, "calculate_microstrip_impedance",
                width_mm=0.4, dielectric_height_mm=0.21)
    assert 40 < res["impedance_ohms"] < 60

    res = _call(mcp, "solve_trace_width_for_impedance",
                target_impedance_ohms=50, dielectric_height_mm=0.21)
    assert 0.2 < res["width_mm"] < 0.5
    assert abs(res["skew_ohms"]) < 0.5

    res = _call(mcp, "calculate_differential_impedance",
                width_mm=0.2, gap_mm=0.2, dielectric_height_mm=0.21)
    assert "differential_impedance_ohms" in res
    assert res["differential_impedance_ohms"] > 0

    res = _call(mcp, "list_impedance_targets")
    assert "USB_2.0_diff" in res["presets"]
    assert "RF_50ohm" in res["presets"]


def test_thermal_tools(board_with_tracks):
    from kicad_claude.tools import thermal
    mcp = _make_mcp([thermal])
    res = _call(mcp, "calculate_trace_current_capacity", width_mm=0.5)
    assert 1.0 < res["current_a"] < 2.0

    res = _call(mcp, "solve_trace_width_for_current", current_a=3.0, copper_oz=1.0)
    assert res["recommended_width_mm"] > 0.5

    res = _call(mcp, "analyze_pcb_current_capacity")
    assert res["total_nets"] >= 1
    # Per-net data populated
    if res["nets"]:
        net = res["nets"][0]
        assert "min_capacity_a" in net
        assert "min_width_mm" in net
        assert "layers" in net


def test_rf_tools(board_with_tracks):
    from kicad_claude.tools import rf as rf_tools, signal_integrity
    mcp = _make_mcp([rf_tools, signal_integrity])

    # add_via_array
    res = _call(mcp, "add_via_array",
                start_x_mm=20, start_y_mm=40, end_x_mm=60, end_y_mm=40,
                spacing_mm=4.0)
    assert res["via_count"] >= 5

    # add_ground_stitching — should add 2 rows
    res = _call(mcp, "add_ground_stitching",
                start_x_mm=20, start_y_mm=50, end_x_mm=60, end_y_mm=50,
                offset_mm=0.5, spacing_mm=4.0)
    assert res["rows"] == 2
    assert res["vias_added"] >= 10

    # add_rf_microstrip
    res = _call(mcp, "add_rf_microstrip",
                start_x_mm=10, start_y_mm=15, end_x_mm=70, end_y_mm=15,
                target_impedance_ohms=50, dielectric_height_mm=0.21)
    assert abs(res["achieved_impedance_ohms"] - 50) < 0.5
    assert 0.2 < res["track_width_mm"] < 0.5


def test_emc_ground_coverage(board_with_tracks):
    from kicad_claude.tools import emc, pcb as pcb_tools
    mcp = _make_mcp([emc, pcb_tools])

    # Add a GND plane
    _call(mcp, "add_ground_plane", layer="B.Cu", net_name="GND")

    res = _call(mcp, "analyze_ground_coverage")
    assert res["board_area_mm2"] > 0
    assert any(layer["layer"] == "B.Cu" for layer in res["by_layer"])


def test_emc_find_long_traces(board_with_tracks):
    from kicad_claude.tools import emc
    mcp = _make_mcp([emc])
    res = _call(mcp, "find_long_traces", threshold_mm=20)
    # We added 3 tracks of 60mm each, all should match
    assert res["count"] >= 1
    assert all(n["length_mm"] >= 20 for n in res["long_nets"])


def test_emc_validate_decoupling_caps(board_with_tracks):
    from kicad_claude.tools import emc
    mcp = _make_mcp([emc])
    res = _call(mcp, "validate_decoupling_caps")
    # No ICs in this test board, so empty results
    assert res["ic_count"] == 0
    assert res["icas_with_decap"] == []
    assert res["icas_missing_decap"] == []


def test_via_array_along_line_helper(board_with_tracks):
    """Direct adapter test: count of vias should match length / spacing."""
    tree = sch_io.parse_file(board_with_tracks["pcb"])
    nodes = pcb_ed.add_via_array_along_line(
        tree, start_mm=(10, 50), end_mm=(70, 50),
        spacing_mm=5.0, drill_mm=0.3, diameter_mm=0.6,
    )
    # 60 mm / 5 mm + 1 = 13 vias (inclusive endpoints)
    assert len(nodes) == 13


def test_via_array_rejects_bad_inputs(board_with_tracks):
    tree = sch_io.parse_file(board_with_tracks["pcb"])
    with pytest.raises(ValueError):
        pcb_ed.add_via_array_along_line(tree, start_mm=(0, 0), end_mm=(10, 0), spacing_mm=0)
    with pytest.raises(ValueError):
        pcb_ed.add_via_array_along_line(tree, start_mm=(0, 0), end_mm=(0, 0), spacing_mm=2)
