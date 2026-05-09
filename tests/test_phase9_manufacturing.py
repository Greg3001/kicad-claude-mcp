"""Phase 9 — manufacturing exports.

Strategy:
- Unit tests: validate adapter invariants (input checks).
- @pytest.mark.slow: build a tiny board via Phase 5 tools, run real
  kicad-cli exports, verify expected files exist.

Skipped when kicad-cli or the library index aren't available.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_claude import state
from kicad_claude.adapters import kicad_cli
from kicad_claude.indexer import kicad_libs
from kicad_claude.templates.blank import write_blank_project
from kicad_claude.tools import library as lib_tools
from kicad_claude.tools import manufacturing as mfg_tools
from kicad_claude.tools import pcb as pcb_tools
from kicad_claude.utils.kicad_paths import find_kicad_cli


# ===== Unit: argument validation =========================================== #


def test_export_pos_rejects_bad_side(tmp_path: Path):
    with pytest.raises(kicad_cli.KicadCliError):
        kicad_cli.export_pos(
            tmp_path / "fake.kicad_pcb", tmp_path / "x.csv", side="upside-down"
        )


def test_export_pos_rejects_bad_format(tmp_path: Path):
    with pytest.raises(kicad_cli.KicadCliError):
        kicad_cli.export_pos(
            tmp_path / "fake.kicad_pcb", tmp_path / "x.bin", fmt="binary"
        )


def test_export_netlist_rejects_unknown_format(tmp_path: Path):
    with pytest.raises(kicad_cli.KicadCliError):
        kicad_cli.export_netlist(
            tmp_path / "fake.kicad_sch", tmp_path / "x.txt", fmt="lispy"
        )


def test_render_pcb_rejects_bad_side(tmp_path: Path):
    with pytest.raises(kicad_cli.KicadCliError):
        kicad_cli.render_pcb(
            tmp_path / "fake.kicad_pcb", tmp_path / "x.png", side="upside"
        )


def test_list_files_returns_empty_for_missing_dir(tmp_path: Path):
    assert kicad_cli._list_files(tmp_path / "nope") == []


# ===== Slow acceptance: real kicad-cli on a tiny board ===================== #


def _have_prerequisites() -> tuple[bool, str]:
    if find_kicad_cli() is None:
        return False, "kicad-cli not available"
    if kicad_libs.load_cache() is None:
        return False, "library index not built"
    return True, ""


@pytest.fixture
def tiny_board(tmp_path: Path):
    """Build a 50×30 board with two SMD resistors + a track."""
    ok, why = _have_prerequisites()
    if not ok:
        pytest.skip(why)
    state.clear_active()
    lib_tools._index = kicad_libs.load_cache()

    files = write_blank_project(tmp_path / "tb", "tb")
    state.set_active(tmp_path / "tb", "tb")

    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP("t")
    pcb_tools.register(mcp)
    mfg_tools.register(mcp)

    def call(name, **kw):
        return mcp._tool_manager.get_tool(name).fn(**kw)

    call("set_board_outline", width_mm=50, height_mm=30)
    call(
        "add_footprint",
        lib_id="Resistor_SMD:R_0603_1608Metric",
        reference="R1", value="10k", x_mm=20, y_mm=15,
    )
    call(
        "add_footprint",
        lib_id="Resistor_SMD:R_0603_1608Metric",
        reference="R2", value="1k", x_mm=35, y_mm=15,
    )
    call("add_track", x1_mm=20, y1_mm=15, x2_mm=35, y2_mm=15)

    yield {"call": call, "files": files, "tmp_path": tmp_path}
    state.clear_active()


@pytest.mark.slow
def test_export_gerbers_writes_per_layer_files(tiny_board):
    res = tiny_board["call"]("export_gerbers")
    assert res["kind"] == "gerbers"
    # Expect at least F.Cu and B.Cu and Edge.Cuts
    names = " ".join(res["files"])
    assert "F_Cu" in names or "F.Cu" in names
    assert "Edge_Cuts" in names or "Edge.Cuts" in names
    assert res["file_count"] >= 8  # at minimum: 2 cu + edge + masks + silks + .gbrjob


@pytest.mark.slow
def test_export_drill_writes_excellon_and_map(tiny_board):
    res = tiny_board["call"]("export_drill")
    assert res["kind"] == "drill"
    names = " ".join(res["files"])
    assert ".drl" in names
    # PTH/NPTH separate by default, plus map files
    assert any("PTH" in f for f in res["files"])
    assert any(".pdf" in f for f in res["files"])  # map_format=pdf default


@pytest.mark.slow
def test_export_pos_csv_has_footprint_rows(tiny_board):
    res = tiny_board["call"]("export_pos", format="csv")
    assert res["kind"] == "pos"
    text = Path(res["output_path"]).read_text()
    # Header + 2 rows for R1, R2
    assert "R1" in text
    assert "R2" in text


@pytest.mark.slow
def test_export_bom_runs_without_schematic_symbols(tiny_board):
    """BOM is empty (no schematic symbols) but must still produce a valid CSV."""
    res = tiny_board["call"]("export_bom")
    assert res["kind"] == "bom"
    assert Path(res["output_path"]).is_file()
    # At least the header row
    assert res["line_count"] >= 1


@pytest.mark.slow
def test_export_netlist_kicadsexpr(tiny_board):
    res = tiny_board["call"]("export_netlist", format="kicadsexpr")
    assert res["kind"] == "netlist"
    text = Path(res["output_path"]).read_text()
    assert text.lstrip().startswith("(")  # s-expression


@pytest.mark.slow
def test_export_pcb_svg_writes_per_layer_files(tiny_board):
    res = tiny_board["call"]("export_pcb_svg", layers="F.Cu,B.Cu,Edge.Cuts")
    assert res["kind"] == "svg"
    assert res["file_count"] >= 1
    names = " ".join(res["files"])
    assert ".svg" in names.lower()


@pytest.mark.slow
def test_render_pcb_3d_top_view(tiny_board):
    res = tiny_board["call"](
        "render_pcb_3d", side="top", width=400, height=300, quality="basic",
    )
    assert res["kind"] == "render"
    p = Path(res["output_path"])
    assert p.is_file()
    assert p.stat().st_size > 1024  # PNG with content, not zero-byte


@pytest.mark.slow
def test_export_fab_package_bundles_everything(tiny_board):
    """One-shot fab package writes gerbers + drill + pos + bom under <project>/fab/."""
    res = tiny_board["call"]("export_fab_package", include_render=False)
    assert "gerbers" in res["steps"]
    assert "drill" in res["steps"]
    assert "pos" in res["steps"]
    assert "bom" in res["steps"]
    # No errors in mandatory steps
    for must_pass in ("gerbers", "drill"):
        assert "error" not in res["steps"][must_pass]
    assert res["total_artifact_count"] >= 10
    base = Path(res["output_dir"])
    assert (base / "gerbers").is_dir()
    assert (base / "drill").is_dir()
