"""Phase 12 — diff pairs, length tuning, schematic buses.

Strategy:
- Pure-Python unit tests on length_tuning math and diff pair detection.
- Tool layer tests with synthetic indices.
- @pytest.mark.slow: real kicad-cli erc/drc on a board with all features.
"""

from __future__ import annotations

import math
import shutil
import subprocess
from pathlib import Path

import pytest

from kicad_claude import state
from kicad_claude.adapters import (
    length_tuning,
    pcb_editor as pcb_ed,
    sch_editor as sch_ed,
    sch_io,
)
from kicad_claude.adapters.sch_io import sym
from kicad_claude.indexer import kicad_libs
from kicad_claude.templates.blank import write_blank_project
from kicad_claude.tools import library as lib_tools
from kicad_claude.utils.kicad_paths import find_kicad_cli


FIXTURES = Path(__file__).parent / "fixtures"


# ===== length_tuning math (pure) =========================================== #


def test_meander_hits_target_length_exactly():
    pts = length_tuning.generate_meander(
        (0, 0), (50, 0), target_length_mm=80, amplitude_mm=2,
    )
    achieved = length_tuning.waypoints_total_length(pts)
    assert math.isclose(achieved, 80.0, abs_tol=0.001)


def test_meander_returns_two_points_when_target_equals_straight():
    pts = length_tuning.generate_meander((0, 0), (10, 0), target_length_mm=10, amplitude_mm=1)
    assert pts == [(0, 0), (10, 0)]


def test_meander_rejects_target_shorter_than_straight():
    with pytest.raises(ValueError, match="shorter"):
        length_tuning.generate_meander((0, 0), (10, 0), target_length_mm=5, amplitude_mm=1)


def test_meander_rejects_when_too_tight():
    with pytest.raises(ValueError, match="needs"):
        length_tuning.generate_meander(
            (0, 0), (10, 0), target_length_mm=200, amplitude_mm=0.5,
        )


def test_meander_at_diagonal_axis():
    """Meander between two non-axis-aligned points still hits target length."""
    pts = length_tuning.generate_meander(
        (0, 0), (30, 40), target_length_mm=80, amplitude_mm=2,
    )
    achieved = length_tuning.waypoints_total_length(pts)
    assert math.isclose(achieved, 80.0, abs_tol=0.01)


def test_meander_side_flips_perpendicular():
    """side=+1 and side=-1 produce mirror images; same length."""
    pts_up = length_tuning.generate_meander(
        (0, 0), (50, 0), target_length_mm=80, amplitude_mm=2, side=1,
    )
    pts_dn = length_tuning.generate_meander(
        (0, 0), (50, 0), target_length_mm=80, amplitude_mm=2, side=-1,
    )
    # Same number of waypoints, same lengths
    assert len(pts_up) == len(pts_dn)
    # Y-coordinates of peaks have opposite signs
    peaks_up = [p[1] for p in pts_up if p[1] != 0]
    peaks_dn = [p[1] for p in pts_dn if p[1] != 0]
    assert all(y > 0 for y in peaks_up)
    assert all(y < 0 for y in peaks_dn)


def test_meander_invalid_amplitude():
    with pytest.raises(ValueError, match="amplitude"):
        length_tuning.generate_meander(
            (0, 0), (10, 0), target_length_mm=20, amplitude_mm=-1,
        )


def test_meander_invalid_side():
    with pytest.raises(ValueError, match="side"):
        length_tuning.generate_meander(
            (0, 0), (10, 0), target_length_mm=20, amplitude_mm=1, side=0,
        )


# ===== diff pair detection (synthetic PCB nets) ============================ #


@pytest.fixture
def pcb_with_named_nets(tmp_path: Path):
    state.clear_active()
    files = write_blank_project(tmp_path / "p", "p")
    state.set_active(tmp_path / "p", "p")
    tree = sch_io.parse_file(files["pcb"])
    for i, name in enumerate([
        "USB_DP", "USB_DM",
        "ETH0_P", "ETH0_N",
        "MIPI_LANE0+", "MIPI_LANE0-",
        "+5V", "GND", "SDA", "SCL",  # not pairs
    ], start=1):
        tree.insert(-1, [sym("net"), i, name])
    sch_io.write_file(files["pcb"], tree)
    yield files
    state.clear_active()


def test_find_diff_pair_candidates_finds_three(pcb_with_named_nets):
    tree = sch_io.parse_file(pcb_with_named_nets["pcb"])
    pairs = pcb_ed.find_diff_pair_candidates(tree)
    base_names = sorted(p["base_name"] for p in pairs)
    assert "USB" in base_names      # DP/DM convention
    assert "ETH0" in base_names     # _P/_N
    assert "MIPI_LANE0" in base_names  # +/-
    assert len(pairs) == 3


def test_find_diff_pair_candidates_skips_unpaired(pcb_with_named_nets):
    """Add a P with no matching N — should not appear in results."""
    tree = sch_io.parse_file(pcb_with_named_nets["pcb"])
    next_idx = max(int(n[1]) for n in sch_io.find_children(tree, "net")) + 1
    tree.insert(-1, [sym("net"), next_idx, "ORPHAN_P"])
    pairs = pcb_ed.find_diff_pair_candidates(tree)
    assert all(p["base_name"] != "ORPHAN" for p in pairs)


# ===== compute_trace_length (synthetic segments) =========================== #


def test_compute_trace_length_sums_segments(tmp_path: Path):
    state.clear_active()
    files = write_blank_project(tmp_path / "p", "p")
    state.set_active(tmp_path / "p", "p")
    tree = sch_io.parse_file(files["pcb"])
    # Declare net 1 = "DATA"
    tree.insert(-1, [sym("net"), 1, "DATA"])
    # Add three segments on net 1
    for (sx, sy, ex, ey) in [(0, 0, 10, 0), (10, 0, 10, 5), (10, 5, 13, 9)]:
        tree.append([
            sym("segment"),
            [sym("start"), sx, sy], [sym("end"), ex, ey],
            [sym("width"), 0.25],
            [sym("layer"), "F.Cu"],
            [sym("net"), 1],
            [sym("uuid"), "u-" + str(sx)],
        ])
    res = pcb_ed.compute_trace_length(tree, "DATA")
    expected = 10.0 + 5.0 + math.hypot(3, 4)  # 10 + 5 + 5
    assert math.isclose(res["total_mm"], expected, abs_tol=0.001)
    assert res["segment_count"] == 3
    assert "F.Cu" in res["by_layer_mm"]
    state.clear_active()


def test_compute_trace_length_unknown_net_raises(tmp_path: Path):
    state.clear_active()
    files = write_blank_project(tmp_path / "p", "p")
    state.set_active(tmp_path / "p", "p")
    tree = sch_io.parse_file(files["pcb"])
    with pytest.raises(KeyError):
        pcb_ed.compute_trace_length(tree, "DOES_NOT_EXIST")
    state.clear_active()


# ===== add_meander_segments writes proper nodes ============================ #


def test_add_meander_segments_emits_chain(tmp_path: Path):
    state.clear_active()
    files = write_blank_project(tmp_path / "p", "p")
    state.set_active(tmp_path / "p", "p")
    tree = sch_io.parse_file(files["pcb"])
    segs = pcb_ed.add_meander_segments(
        tree,
        start_mm=(20, 50), end_mm=(60, 50),
        target_length_mm=60, amplitude_mm=2,
        layer="F.Cu",
    )
    # Achieved length should match target (within float tolerance)
    total = 0.0
    for seg in segs:
        s = sch_io.find_child(seg, "start")
        e = sch_io.find_child(seg, "end")
        total += math.hypot(float(e[1]) - float(s[1]), float(e[2]) - float(s[2]))
    assert math.isclose(total, 60.0, abs_tol=0.01)
    state.clear_active()


# ===== Bus helpers (sch_editor) ============================================ #


@pytest.fixture
def blank_sch(tmp_path: Path):
    state.clear_active()
    files = write_blank_project(tmp_path / "p", "p")
    state.set_active(tmp_path / "p", "p")
    yield files
    state.clear_active()


def test_add_bus_segment_appends_node(blank_sch):
    tree = sch_io.parse_file(blank_sch["sch"])
    sch_ed.add_bus_segment(tree, 50, 50, 100, 50)
    assert len(sch_io.find_children(tree, "bus")) == 1


def test_add_bus_entry_validates_direction(blank_sch):
    tree = sch_io.parse_file(blank_sch["sch"])
    sch_ed.add_bus_entry(tree, 60, 50, direction="right_down")
    assert len(sch_io.find_children(tree, "bus_entry")) == 1
    with pytest.raises(ValueError):
        sch_ed.add_bus_entry(tree, 70, 50, direction="upside-down")


def test_add_bus_alias_writes_members(blank_sch):
    tree = sch_io.parse_file(blank_sch["sch"])
    sch_ed.add_bus_alias(tree, "DATA", [f"D{i}" for i in range(8)])
    aliases = sch_io.find_children(tree, "bus_alias")
    assert len(aliases) == 1
    assert aliases[0][1] == "DATA"
    members = sch_io.find_child(aliases[0], "members")
    member_names = [m for m in members[1:] if isinstance(m, str)]
    assert len(member_names) == 8


def test_add_bus_alias_rejects_empty_members(blank_sch):
    tree = sch_io.parse_file(blank_sch["sch"])
    with pytest.raises(ValueError):
        sch_ed.add_bus_alias(tree, "X", [])


# ===== Tool layer (FastMCP) ================================================ #


def _make_pcb_mcp(monkeypatch):
    cached = kicad_libs.load_cache()
    if cached is None:
        pytest.skip("library index not built")
    from mcp.server.fastmcp import FastMCP
    monkeypatch.setattr(lib_tools, "load_cache", lambda: cached)
    monkeypatch.setattr(lib_tools, "_index", None)
    mcp = FastMCP("t")
    from kicad_claude.tools import pcb as pcb_tools
    pcb_tools.register(mcp)
    return mcp


def _call(mcp, _name, **kw):
    return mcp._tool_manager.get_tool(_name).fn(**kw)


def test_list_diff_pair_candidates_tool(pcb_with_named_nets, monkeypatch):
    mcp = _make_pcb_mcp(monkeypatch)
    res = _call(mcp, "list_diff_pair_candidates")
    assert len(res["pairs"]) == 3


def test_validate_diff_pair_length_match_tool(pcb_with_named_nets, monkeypatch):
    """USB_DP has 10 mm of trace, USB_DM has 12 mm — 2 mm skew."""
    tree = sch_io.parse_file(pcb_with_named_nets["pcb"])
    # Find net indices for USB_DP and USB_DM
    dp_idx = pcb_ed.find_net_index(tree, "USB_DP")
    dm_idx = pcb_ed.find_net_index(tree, "USB_DM")
    tree.append([
        sym("segment"),
        [sym("start"), 0, 0], [sym("end"), 10, 0],
        [sym("width"), 0.25], [sym("layer"), "F.Cu"],
        [sym("net"), dp_idx],
        [sym("uuid"), "a"],
    ])
    tree.append([
        sym("segment"),
        [sym("start"), 0, 0], [sym("end"), 12, 0],
        [sym("width"), 0.25], [sym("layer"), "F.Cu"],
        [sym("net"), dm_idx],
        [sym("uuid"), "b"],
    ])
    sch_io.write_file(pcb_with_named_nets["pcb"], tree)

    mcp = _make_pcb_mcp(monkeypatch)
    res = _call(
        mcp, "validate_diff_pair_length_match",
        positive_net="USB_DP", negative_net="USB_DM", tolerance_mm=0.5,
    )
    assert res["positive_length_mm"] == 10.0
    assert res["negative_length_mm"] == 12.0
    assert res["skew_mm"] == 2.0
    assert res["within_tolerance"] is False
    assert res["longer_net"] == "USB_DM"


def test_add_meander_tool(pcb_with_named_nets, monkeypatch):
    mcp = _make_pcb_mcp(monkeypatch)
    res = _call(
        mcp, "add_meander",
        x1_mm=20, y1_mm=20, x2_mm=60, y2_mm=20,
        target_length_mm=55, amplitude_mm=2, side="up",
        net_name="USB_DP",
    )
    assert math.isclose(res["achieved_length_mm"], 55.0, abs_tol=0.01)
    assert res["segment_count"] >= 5


# ===== Acceptance: real kicad-cli on a board with diff pairs + meander ===== #


@pytest.mark.slow
def test_acceptance_phase12_full(tmp_path: Path):
    if find_kicad_cli() is None:
        pytest.skip("kicad-cli not available")
    cached = kicad_libs.load_cache()
    if cached is None:
        pytest.skip("library index not built")
    state.clear_active()
    lib_tools._index = cached

    files = write_blank_project(tmp_path / "f", "f")
    state.set_active(tmp_path / "f", "f")

    # Inject named nets directly so diff pair detection has something to find.
    tree = sch_io.parse_file(files["pcb"])
    for i, name in enumerate(
        ["USB_DP", "USB_DM", "+5V", "GND"], start=1,
    ):
        tree.insert(-1, [sym("net"), i, name])
    sch_io.write_file(files["pcb"], tree)

    from mcp.server.fastmcp import FastMCP
    from kicad_claude.tools import (
        library, pcb as pcb_tools, rules, schematic, validation,
    )
    mcp = FastMCP("t")
    for mod in (library, pcb_tools, rules, schematic, validation):
        mod.register(mcp)
    def call(_tool, **kw):
        return mcp._tool_manager.get_tool(_tool).fn(**kw)

    # PCB: outline + diff pair class + meander
    call("set_board_outline", width_mm=80, height_mm=60)
    call(
        "add_diff_pair_class",
        name="USB",
        diff_pair_width_mm=0.2, diff_pair_gap_mm=0.18,
    )
    call("assign_net_class", net_pattern="USB_*", class_name="USB")
    call("add_meander",
         x1_mm=20, y1_mm=30, x2_mm=60, y2_mm=30,
         target_length_mm=55, amplitude_mm=2, side="up", net_name="USB_DP")

    # Schematic: add a bus + alias
    call("add_bus", x1_mm=80, y1_mm=130, x2_mm=160, y2_mm=130)
    call("add_bus_alias", alias_name="DATA", members=[f"D{i}" for i in range(8)])

    # Both files must still parse.
    cli = find_kicad_cli()
    r = subprocess.run(
        [str(cli), "sch", "erc", "--format", "json", str(files["sch"])],
        capture_output=True, text=True, timeout=60, cwd=tmp_path,
    )
    assert r.returncode == 0
    r = subprocess.run(
        [str(cli), "pcb", "drc", "--format", "json", str(files["pcb"])],
        capture_output=True, text=True, timeout=60, cwd=tmp_path,
    )
    assert r.returncode == 0
    state.clear_active()
