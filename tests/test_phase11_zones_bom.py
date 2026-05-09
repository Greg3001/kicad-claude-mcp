"""Phase 11 — copper zones, mounting holes, silk text, fiducials, BOM sourcing.

Strategy:
- Unit tests on the editor and BOM helpers (no kicad-cli, no network).
- @pytest.mark.slow: real kicad-cli pcb drc with --refill-zones on a board
  with a GND plane.
- @pytest.mark.network: live BOM enrichment against DigiKey + Mouser.
"""

from __future__ import annotations

import csv
import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

from kicad_claude import state
from kicad_claude.adapters import pcb_editor as pcb_ed
from kicad_claude.adapters import sch_io
from kicad_claude.indexer import kicad_libs
from kicad_claude.templates.blank import write_blank_project
from kicad_claude.tools import library as lib_tools
from kicad_claude.tools import pcb as pcb_tools
from kicad_claude.tools import sourcing as sourcing_tools
from kicad_claude.utils.kicad_paths import find_kicad_cli

load_dotenv(Path(__file__).resolve().parents[1] / ".env")


# ===== pcb_editor unit ====================================================== #


@pytest.fixture
def blank_project(tmp_path: Path):
    state.clear_active()
    files = write_blank_project(tmp_path / "p", "p")
    state.set_active(tmp_path / "p", "p")
    yield files
    state.clear_active()


def test_get_board_outline_polygon_after_set_outline(blank_project):
    tree = sch_io.parse_file(blank_project["pcb"])
    pcb_ed.set_board_outline(tree, 50, 30)
    poly = pcb_ed.get_board_outline_polygon_kicad(tree)
    assert poly is not None
    assert len(poly) == 4
    # MCP corners (Y up): bottom-left (10,10), top-right (60,40).
    # Convert to KiCAD (Y down, page_h=210): (10, 200) and (60, 170).
    xs = sorted({p[0] for p in poly})
    ys = sorted({p[1] for p in poly})
    assert xs == [10.0, 60.0]
    assert ys == [170.0, 200.0]


def test_get_board_outline_polygon_returns_none_when_no_outline(blank_project):
    tree = sch_io.parse_file(blank_project["pcb"])
    assert pcb_ed.get_board_outline_polygon_kicad(tree) is None


def test_add_zone_appends_node_with_polygon(blank_project):
    tree = sch_io.parse_file(blank_project["pcb"])
    pcb_ed.set_board_outline(tree, 50, 30)
    poly = [(20, 20), (40, 20), (40, 25), (20, 25)]
    pcb_ed.add_zone(tree, net_name="GND", layer="F.Cu", polygon_mcp=poly)

    zones = sch_io.find_children(tree, "zone")
    assert len(zones) == 1
    z = zones[0]
    layer_node = sch_io.find_child(z, "layer")
    assert layer_node[1] == "F.Cu"
    net_name = sch_io.find_child(z, "net_name")
    assert net_name[1] == "GND"


def test_add_zone_rejects_under_3_vertices(blank_project):
    tree = sch_io.parse_file(blank_project["pcb"])
    with pytest.raises(ValueError, match="3 points"):
        pcb_ed.add_zone(
            tree, net_name="GND", layer="F.Cu",
            polygon_mcp=[(0, 0), (10, 0)],
        )


def test_add_ground_plane_uses_board_outline(blank_project):
    tree = sch_io.parse_file(blank_project["pcb"])
    pcb_ed.set_board_outline(tree, 50, 30)
    pcb_ed.add_ground_plane(tree)
    zones = sch_io.find_children(tree, "zone")
    assert len(zones) == 1
    poly = sch_io.find_child(zones[0], "polygon")
    pts = sch_io.find_child(poly, "pts")
    xy_count = sum(1 for c in pts[1:] if sch_io.is_call(c, "xy"))
    assert xy_count == 4


def test_add_ground_plane_without_outline_raises(blank_project):
    tree = sch_io.parse_file(blank_project["pcb"])
    with pytest.raises(RuntimeError, match="board outline"):
        pcb_ed.add_ground_plane(tree)


def test_add_silk_text_writes_gr_text(blank_project):
    tree = sch_io.parse_file(blank_project["pcb"])
    pcb_ed.add_silk_text(tree, text="REV 1.0", x_mm=20, y_mm=20)
    texts = sch_io.find_children(tree, "gr_text")
    assert len(texts) == 1
    assert texts[0][1] == "REV 1.0"
    layer = sch_io.find_child(texts[0], "layer")
    assert layer[1] == "F.SilkS"


# ===== Tool layer ========================================================== #


def _patched_index(tmp_path):
    """Build a small index with the official KiCAD libs we need for Phase 11."""
    cached = kicad_libs.load_cache()
    if cached is None:
        pytest.skip("library index not built")
    return cached


def _make_mcp(monkeypatch, tmp_path):
    from mcp.server.fastmcp import FastMCP
    idx = _patched_index(tmp_path)
    monkeypatch.setattr(lib_tools, "load_cache", lambda: idx)
    monkeypatch.setattr(lib_tools, "_index", None)
    mcp = FastMCP("t")
    pcb_tools.register(mcp)
    return mcp


def _call(mcp, _name, **kw):
    return mcp._tool_manager.get_tool(_name).fn(**kw)


def test_add_zone_tool(blank_project, tmp_path, monkeypatch):
    mcp = _make_mcp(monkeypatch, tmp_path)
    _call(mcp, "set_board_outline", width_mm=50, height_mm=30)
    res = _call(
        mcp, "add_zone",
        net_name="GND", layer="F.Cu",
        polygon_mm=[[10, 10], [60, 10], [60, 40], [10, 40]],
    )
    assert res["net_name"] == "GND"
    assert res["vertices"] == 4

    tree = sch_io.parse_file(blank_project["pcb"])
    assert len(sch_io.find_children(tree, "zone")) == 1


def test_add_silk_text_tool(blank_project, tmp_path, monkeypatch):
    mcp = _make_mcp(monkeypatch, tmp_path)
    _call(mcp, "add_silk_text", text="DEMO", x_mm=20, y_mm=20)
    tree = sch_io.parse_file(blank_project["pcb"])
    assert len(sch_io.find_children(tree, "gr_text")) == 1


def test_add_mounting_hole_tool_resolves_lib_id(blank_project, tmp_path, monkeypatch):
    mcp = _make_mcp(monkeypatch, tmp_path)
    res = _call(mcp, "add_mounting_hole", x_mm=10, y_mm=10, diameter_mm=3.2, plated=True)
    assert "MountingHole_3.2mm" in res["lib_id"]
    assert res["plated"] is True
    assert res["reference"] == "H1"
    # Adding a second auto-increments
    res2 = _call(mcp, "add_mounting_hole", x_mm=20, y_mm=10, diameter_mm=3.2, plated=True)
    assert res2["reference"] == "H2"


def test_add_mounting_hole_unknown_diameter_raises(blank_project, tmp_path, monkeypatch):
    mcp = _make_mcp(monkeypatch, tmp_path)
    with pytest.raises(FileNotFoundError, match="MountingHole"):
        _call(mcp, "add_mounting_hole", x_mm=10, y_mm=10, diameter_mm=99.9)


def test_add_fiducial_tool(blank_project, tmp_path, monkeypatch):
    mcp = _make_mcp(monkeypatch, tmp_path)
    res = _call(mcp, "add_fiducial", x_mm=5, y_mm=5, size="1mm")
    assert res["size"] == "1mm"
    assert res["reference"] == "FID1"
    assert "Fiducial" in res["lib_id"]


def test_add_fiducial_invalid_size(blank_project, tmp_path, monkeypatch):
    mcp = _make_mcp(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="size must be"):
        _call(mcp, "add_fiducial", x_mm=5, y_mm=5, size="2mm")


# ===== BOM enrichment (no network) ========================================= #


def _write_fake_bom(path: Path):
    """Make a fake KiCAD-style BOM."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Refs", "Value", "Footprint", "Qty", "DNP"])
        w.writerow(["R1", "10k", "", "1", ""])
        w.writerow(["U1", "LM358N", "", "1", ""])


def test_enrich_bom_with_offline_stubs(blank_project, tmp_path, monkeypatch):
    """Stub out DigiKey + Mouser; verify CSV columns are appended correctly."""
    bom = blank_project["pro"].parent / "fab" / "p-bom.csv"
    _write_fake_bom(bom)

    monkeypatch.setattr(
        sourcing_tools.digikey, "search_keyword",
        lambda q, limit=1: [{
            "source": "digikey", "mpn": f"{q}-DK", "manufacturer": "DK Mfr",
            "stock": 100, "unit_price": 0.5, "currency": "EUR",
            "product_url": "https://digikey.example/" + q,
            "datasheet_url": "", "category": "",
        }],
    )
    monkeypatch.setattr(
        sourcing_tools.mouser, "search_part",
        lambda q: [{
            "source": "mouser", "mpn": f"{q}-MO", "manufacturer": "MO Mfr",
            "stock": 50, "unit_price": 0.7, "currency": "EUR",
            "product_url": "https://mouser.example/" + q,
            "datasheet_url": "", "lead_time": "",
        }],
    )

    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP("t")
    sourcing_tools.register(mcp)
    res = _call(
        mcp, "enrich_bom_with_sourcing",
        bom_path=str(bom),
        output_path=str(tmp_path / "out.csv"),
    )
    assert res["row_count"] == 2
    assert res["digikey_hits"] == 2
    assert res["mouser_hits"] == 2

    out = (tmp_path / "out.csv").read_text()
    assert "dk_mpn" in out
    assert "mo_mpn" in out
    assert "10k-DK" in out
    assert "LM358N-MO" in out


def test_enrich_bom_unknown_field_raises(blank_project, tmp_path, monkeypatch):
    bom = blank_project["pro"].parent / "fab" / "p-bom.csv"
    _write_fake_bom(bom)
    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP("t")
    sourcing_tools.register(mcp)
    with pytest.raises(ValueError, match="no field"):
        _call(
            mcp, "enrich_bom_with_sourcing",
            bom_path=str(bom),
            sourcing_field="MPN",  # not in our fake BOM
        )


# ===== Slow: real kicad-cli ================================================ #


@pytest.mark.slow
def test_acceptance_gnd_plane_drc_clean(tmp_path: Path):
    if find_kicad_cli() is None:
        pytest.skip("kicad-cli not available")
    cached = kicad_libs.load_cache()
    if cached is None:
        pytest.skip("library index not built")

    state.clear_active()
    lib_tools._index = cached

    files = write_blank_project(tmp_path / "g", "g")
    state.set_active(tmp_path / "g", "g")

    from mcp.server.fastmcp import FastMCP
    from kicad_claude.tools import validation as val_tools
    mcp = FastMCP("t")
    pcb_tools.register(mcp)
    val_tools.register(mcp)

    def call(name, **kw):
        return mcp._tool_manager.get_tool(name).fn(**kw)

    call("set_board_outline", width_mm=80, height_mm=60)
    call("add_footprint",
         lib_id="Resistor_SMD:R_0603_1608Metric",
         reference="R1", value="10k", x_mm=20, y_mm=20)
    call("add_silk_text", text="REV 1.0", x_mm=40, y_mm=10, size_mm=1.5)
    for i, (x, y) in enumerate([(15, 15), (75, 15), (15, 55), (75, 55)], 1):
        call("add_mounting_hole", x_mm=x, y_mm=y, diameter_mm=3.2, plated=True)
    call("add_fiducial", x_mm=5, y_mm=5, size="1mm")
    call("add_ground_plane", layer="B.Cu", net_name="GND")

    drc = call("run_drc", refill_zones=True, schematic_parity=False)
    # We expect 0 errors — warnings are OK (fab tolerances).
    assert drc["errors"] == 0
    state.clear_active()


# ===== Live network ======================================================= #


def _have_creds(*names) -> bool:
    return all(os.environ.get(n) for n in names)


@pytest.mark.network
@pytest.mark.skipif(
    not _have_creds("DIGIKEY_CLIENT_ID", "DIGIKEY_CLIENT_SECRET"),
    reason="DigiKey credentials missing",
)
def test_acceptance_enrich_bom_live(tmp_path: Path):
    """Hit live DigiKey for a real part (LM358N) and confirm fields populated."""
    bom = tmp_path / "bom.csv"
    _write_fake_bom(bom)

    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP("t")
    sourcing_tools.register(mcp)

    state.clear_active()
    files = write_blank_project(tmp_path / "p", "p")
    state.set_active(tmp_path / "p", "p")
    try:
        res = _call(
            mcp, "enrich_bom_with_sourcing",
            bom_path=str(bom),
            output_path=str(tmp_path / "out.csv"),
            sources="digikey",  # mouser optional
            max_rows=5,
        )
        assert res["digikey_hits"] >= 1, "expected at least one DigiKey hit"
        out = (tmp_path / "out.csv").read_text()
        assert "LM358" in out
    finally:
        state.clear_active()
