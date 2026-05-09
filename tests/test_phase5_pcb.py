"""Phase 5 — PCB editing.

Strategy:
- Unit tests against the blank-template PCB and a synthetic footprint def.
- Integration test: build a tiny board with real KiCAD library footprints
  and verify kicad-cli pcb drc parses it (marked @pytest.mark.slow).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from kicad_claude import state
from kicad_claude.adapters import pcb_editor as ed
from kicad_claude.adapters import sch_io
from kicad_claude.indexer import kicad_libs
from kicad_claude.templates.blank import write_blank_project
from kicad_claude.tools import library as lib_tools
from kicad_claude.tools import pcb as pcb_tools
from kicad_claude.utils.kicad_paths import find_kicad_cli, find_footprint_lib_dirs

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def blank_project(tmp_path: Path):
    state.clear_active()
    files = write_blank_project(tmp_path / "demo", "demo")
    state.set_active(tmp_path / "demo", "demo")
    yield files
    state.clear_active()


# ===== Board outline ======================================================= #


def test_set_board_outline_appends_gr_rect_on_edge_cuts(blank_project):
    pcb = blank_project["pcb"]
    tree = sch_io.parse_file(pcb)
    result = ed.set_board_outline(tree, 50, 30)
    assert result["width_mm"] == 50
    assert result["height_mm"] == 30

    rects = sch_io.find_children(tree, "gr_rect")
    edge_rects = [r for r in rects if (sch_io.find_child(r, "layer") or [None, ""])[1] == "Edge.Cuts"]
    assert len(edge_rects) == 1


def test_set_board_outline_replaces_previous_outline(blank_project):
    tree = sch_io.parse_file(blank_project["pcb"])
    ed.set_board_outline(tree, 50, 30)
    ed.set_board_outline(tree, 80, 40)
    edge_rects = [
        r for r in sch_io.find_children(tree, "gr_rect")
        if (sch_io.find_child(r, "layer") or [None, ""])[1] == "Edge.Cuts"
    ]
    assert len(edge_rects) == 1


def test_set_board_outline_rejects_unknown_shape(blank_project):
    tree = sch_io.parse_file(blank_project["pcb"])
    with pytest.raises(ValueError):
        ed.set_board_outline(tree, 50, 30, shape="circle")


# ===== Footprint placement (synthetic .kicad_mod) ========================== #


def _make_synth_fp_def() -> list:
    """Parse the MiniFP fixture as a footprint def."""
    return ed.fetch_footprint_def(FIXTURES / "MiniFP.pretty" / "Mini_R_0603.kicad_mod")


def test_add_footprint_via_editor(blank_project):
    tree = sch_io.parse_file(blank_project["pcb"])
    fp_def = _make_synth_fp_def()
    ed.add_footprint(
        tree,
        qualified_lib_id="MiniFP:Mini_R_0603",
        reference="R1",
        value="10k",
        x_mm=20,
        y_mm=15,
        rotation=0,
        layer="F.Cu",
        fp_def_node=fp_def,
    )
    fp = ed.find_footprint_by_reference(tree, "R1")
    assert fp is not None
    layer = sch_io.find_child(fp, "layer")
    assert layer[1] == "F.Cu"
    at = sch_io.find_child(fp, "at")
    # MCP (20, 15) -> KiCAD (20, 210-15=195) for A4 landscape (210mm)
    assert at[1] == 20.0
    assert at[2] == 195.0


def test_add_footprint_rejects_bad_layer(blank_project):
    tree = sch_io.parse_file(blank_project["pcb"])
    with pytest.raises(ValueError, match="layer"):
        ed.add_footprint(
            tree, qualified_lib_id="x:y", reference="R1", value="v",
            x_mm=0, y_mm=0, layer="F.Bogus", fp_def_node=_make_synth_fp_def()
        )


def test_duplicate_footprint_reference_rejected(blank_project):
    tree = sch_io.parse_file(blank_project["pcb"])
    fp_def = _make_synth_fp_def()
    ed.add_footprint(
        tree, qualified_lib_id="MiniFP:Mini_R_0603", reference="R1",
        value="10k", x_mm=10, y_mm=10, fp_def_node=fp_def,
    )
    with pytest.raises(ValueError, match="already exists"):
        ed.add_footprint(
            tree, qualified_lib_id="MiniFP:Mini_R_0603", reference="R1",
            value="other", x_mm=20, y_mm=20, fp_def_node=_make_synth_fp_def(),
        )


def test_remove_footprint(blank_project):
    tree = sch_io.parse_file(blank_project["pcb"])
    fp_def = _make_synth_fp_def()
    ed.add_footprint(
        tree, qualified_lib_id="MiniFP:Mini_R_0603", reference="R1",
        value="10k", x_mm=10, y_mm=10, fp_def_node=fp_def,
    )
    assert ed.remove_footprint(tree, "R1") is True
    assert ed.remove_footprint(tree, "DNE") is False


def test_move_footprint(blank_project):
    tree = sch_io.parse_file(blank_project["pcb"])
    fp_def = _make_synth_fp_def()
    ed.add_footprint(
        tree, qualified_lib_id="MiniFP:Mini_R_0603", reference="R1",
        value="10k", x_mm=10, y_mm=10, fp_def_node=fp_def,
    )
    ed.move_footprint(tree, "R1", x_mm=30, y_mm=25, rotation=90, layer="B.Cu")
    fp = ed.find_footprint_by_reference(tree, "R1")
    at = sch_io.find_child(fp, "at")
    layer = sch_io.find_child(fp, "layer")
    assert at[1] == 30.0
    assert at[2] == 185.0  # 210 - 25
    assert at[3] == 90
    assert layer[1] == "B.Cu"


# ===== place_footprints_grid =============================================== #


def test_place_footprints_grid_distributes_unplaced(blank_project):
    tree = sch_io.parse_file(blank_project["pcb"])
    # Simulate "Update PCB from Schematic" — KiCAD stacks footprints at
    # KiCAD-(0, 0). We add via the editor (which uses MCP coords) then force
    # the underlying `at` to (0, 0) to match the real GUI state.
    for ref in ("R1", "R2", "R3", "C1", "C2"):
        ed.add_footprint(
            tree, qualified_lib_id="MiniFP:Mini_R_0603", reference=ref,
            value="x", x_mm=0, y_mm=0,
            fp_def_node=ed.fetch_footprint_def(
                FIXTURES / "MiniFP.pretty" / "Mini_R_0603.kicad_mod"
            ),
        )
        at = sch_io.find_child(ed.find_footprint_by_reference(tree, ref), "at")
        at[1] = 0.0
        at[2] = 0.0

    summary = ed.place_footprints_grid(tree, spacing_mm=10, columns=3, origin_mcp=(15, 15))
    assert summary["placed"] == 5
    # All footprints should now have unique non-origin positions.
    positions = []
    for fp in ed.iter_footprints(tree):
        at = sch_io.find_child(fp, "at")
        positions.append((float(at[1]), float(at[2])))
    assert len(set(positions)) == 5
    # All footprints now have unique non-origin positions
    positions = []
    for fp in ed.iter_footprints(tree):
        at = sch_io.find_child(fp, "at")
        positions.append((float(at[1]), float(at[2])))
    assert len(set(positions)) == 5


# ===== Tracks / vias ======================================================= #


def test_add_track_persists(blank_project):
    tree = sch_io.parse_file(blank_project["pcb"])
    ed.add_track(tree, 10, 10, 30, 10, width_mm=0.25)
    sch_io.write_file(blank_project["pcb"], tree)
    tree2 = sch_io.parse_file(blank_project["pcb"])
    segs = sch_io.find_children(tree2, "segment")
    assert len(segs) == 1
    assert sch_io.find_child(segs[0], "width")[1] == 0.25


def test_add_via_persists(blank_project):
    tree = sch_io.parse_file(blank_project["pcb"])
    ed.add_via(tree, 20, 15, drill_mm=0.4, diameter_mm=0.8)
    sch_io.write_file(blank_project["pcb"], tree)
    tree2 = sch_io.parse_file(blank_project["pcb"])
    vias = sch_io.find_children(tree2, "via")
    assert len(vias) == 1
    drill = sch_io.find_child(vias[0], "drill")
    assert drill[1] == 0.4


# ===== Tool layer ========================================================== #


def _patched_index(tmp_path):
    """Build an index pointing at the MiniFP fixture."""
    fp_dir = tmp_path / "fps"
    fp_dir.mkdir()
    shutil.copytree(FIXTURES / "MiniFP.pretty", fp_dir / "MiniFP.pretty")
    return kicad_libs.build_index(symbol_dirs=[], footprint_dirs=[fp_dir])


def _make_mcp(monkeypatch, tmp_path):
    from mcp.server.fastmcp import FastMCP
    idx = _patched_index(tmp_path)
    monkeypatch.setattr(lib_tools, "load_cache", lambda: idx)
    monkeypatch.setattr(lib_tools, "_index", None)
    mcp = FastMCP("t")
    pcb_tools.register(mcp)
    return mcp


def _call(mcp, name, **kwargs):
    return mcp._tool_manager.get_tool(name).fn(**kwargs)


def test_pcb_tools_end_to_end(blank_project, tmp_path, monkeypatch):
    mcp = _make_mcp(monkeypatch, tmp_path)
    _call(mcp, "set_board_outline", width_mm=50, height_mm=30)
    _call(mcp, "add_footprint", lib_id="MiniFP:Mini_R_0603",
          reference="R1", value="10k", x_mm=20, y_mm=20)
    _call(mcp, "add_footprint", lib_id="MiniFP:Mini_R_0603",
          reference="R2", value="1k", x_mm=40, y_mm=20)
    _call(mcp, "add_track", x1_mm=20, y1_mm=20, x2_mm=40, y2_mm=20)
    _call(mcp, "add_via", x_mm=30, y_mm=20)

    fps = _call(mcp, "list_footprints")
    assert {fp["reference"] for fp in fps} == {"R1", "R2"}

    # Read schematic back to confirm persistence
    tree = sch_io.parse_file(blank_project["pcb"])
    assert len(sch_io.find_children(tree, "footprint")) == 2
    assert len(sch_io.find_children(tree, "segment")) == 1
    assert len(sch_io.find_children(tree, "via")) == 1


def test_move_footprint_via_tool(blank_project, tmp_path, monkeypatch):
    mcp = _make_mcp(monkeypatch, tmp_path)
    _call(mcp, "add_footprint", lib_id="MiniFP:Mini_R_0603",
          reference="R1", value="v", x_mm=20, y_mm=20)
    _call(mcp, "move_footprint", reference="R1", x_mm=30, y_mm=25, rotation=180)
    fps = _call(mcp, "list_footprints")
    r1 = next(fp for fp in fps if fp["reference"] == "R1")
    assert r1["position_mm"] == [30.0, 25.0]


# ===== Acceptance: small board with KiCAD official footprints ============== #


@pytest.mark.slow
def test_acceptance_small_board_with_real_libs(tmp_path):
    """Build a 50x30 board with 2 SMD resistors + a track, then run kicad-cli pcb drc."""
    cli = find_kicad_cli()
    if not cli or not find_footprint_lib_dirs():
        pytest.skip("kicad-cli or footprint libs not available")

    cached = kicad_libs.load_cache()
    if cached is None:
        pytest.skip("library index not built; run index_libraries first")
    state.clear_active()
    lib_tools._index = cached

    files = write_blank_project(tmp_path / "tinyboard", "tinyboard")
    state.set_active(tmp_path / "tinyboard", "tinyboard")

    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP("t")
    pcb_tools.register(mcp)

    _call(mcp, "set_board_outline", width_mm=50, height_mm=30)
    _call(mcp, "add_footprint",
          lib_id="Resistor_SMD:R_0603_1608Metric", reference="R1",
          value="10k", x_mm=20, y_mm=20)
    _call(mcp, "add_footprint",
          lib_id="Resistor_SMD:R_0603_1608Metric", reference="R2",
          value="1k", x_mm=35, y_mm=20)
    _call(mcp, "add_track", x1_mm=20, y1_mm=20, x2_mm=35, y2_mm=20)

    pcb_path = files["pcb"]
    state.clear_active()

    r = subprocess.run(
        [str(cli), "pcb", "drc", str(pcb_path)],
        capture_output=True, text=True, timeout=60, cwd=tmp_path,
    )
    # DRC will report unconnected/clearance issues but the file MUST parse.
    assert r.returncode == 0, f"drc parse failed: stderr={r.stderr}"
