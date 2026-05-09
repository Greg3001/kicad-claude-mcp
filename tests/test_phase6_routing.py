"""Phase 6 — autorouting via Freerouting.

Strategy:
- Unit tests for path detection and stats parsing (no network, no Java).
- @pytest.mark.slow: full pipeline (export DSN → Freerouting → import SES)
  on a small board built with KiCAD official footprints. Skipped if any
  prerequisite (KiCAD bundled Python, Freerouting JAR, library index) is
  missing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_claude import state
from kicad_claude.adapters import freerouting, kicad_python
from kicad_claude.indexer import kicad_libs
from kicad_claude.templates.blank import write_blank_project
from kicad_claude.tools import library as lib_tools
from kicad_claude.tools import pcb as pcb_tools
from kicad_claude.tools import routing as routing_tools


# ===== Path detection ====================================================== #


def test_find_kicad_python_returns_existing_path():
    p = kicad_python.find_kicad_python()
    if p is None:
        pytest.skip("KiCAD bundled Python not present on this system")
    assert p.is_file()


def test_find_freerouting_jar_returns_existing_path():
    p = freerouting.find_freerouting_jar()
    if p is None:
        pytest.skip("freerouting.jar not present (not yet downloaded?)")
    assert p.is_file()
    # Sanity: jar files are at least a few MB.
    assert p.stat().st_size > 1_000_000


def test_find_java_present_in_environment():
    j = freerouting.find_java()
    if j is None:
        pytest.skip("`java` not on PATH")
    assert j.is_file()


# ===== Stats parser ======================================================== #


SAMPLE_LOG = """
2026-05-09 21:00:00 INFO  Freerouting v2.1.0 starting
2026-05-09 21:00:01 INFO  Loaded design: foo.dsn
2026-05-09 21:00:05 INFO  pass 1 complete: 12% completed
2026-05-09 21:00:30 INFO  pass 5 complete: 87% completed
2026-05-09 21:00:35 INFO  Routing took 0:00:35 to complete
2026-05-09 21:00:35 INFO  Total trace length: 287.5 mm
2026-05-09 21:00:35 INFO  Total of 12 vias
"""


def test_parse_stats_extracts_known_fields():
    stats = freerouting._parse_stats(SAMPLE_LOG)
    assert stats.get("via_count") == 12
    assert stats.get("trace_length_mm") == 287.5
    assert stats.get("duration") == "0:00:35"
    # `passes_done` and `completion_pct` take the LAST match (final state).
    assert stats.get("passes_done") == 5
    assert stats.get("completion_pct") == 87.0


def test_parse_stats_empty_when_no_matches():
    assert freerouting._parse_stats("nothing relevant here") == {}


# ===== Full pipeline (slow) ================================================ #


def _have_prerequisites() -> tuple[bool, str]:
    if kicad_python.find_kicad_python() is None:
        return False, "KiCAD bundled Python not found"
    if freerouting.find_freerouting_jar() is None:
        return False, "freerouting.jar not found"
    if freerouting.find_java() is None:
        return False, "java not on PATH"
    if kicad_libs.load_cache() is None:
        return False, "library index not built"
    return True, ""


@pytest.mark.slow
def test_export_dsn_then_import_ses_pipeline(tmp_path: Path):
    """Build a tiny board, export DSN, run Freerouting, import SES — file grows."""
    ok, why = _have_prerequisites()
    if not ok:
        pytest.skip(why)

    state.clear_active()
    lib_tools._index = kicad_libs.load_cache()

    files = write_blank_project(tmp_path / "rt", "rt")
    state.set_active(tmp_path / "rt", "rt")

    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP("t")
    pcb_tools.register(mcp)
    routing_tools.register(mcp)

    def call(name, **kw):
        return mcp._tool_manager.get_tool(name).fn(**kw)

    call("set_board_outline", width_mm=50, height_mm=30)
    call("add_footprint", lib_id="Resistor_SMD:R_0603_1608Metric",
         reference="R1", value="10k", x_mm=20, y_mm=15)
    call("add_footprint", lib_id="Resistor_SMD:R_0603_1608Metric",
         reference="R2", value="1k", x_mm=35, y_mm=15)

    pcb_path = files["pcb"]
    size_before_export = pcb_path.stat().st_size

    # 1) Export DSN
    res = call("export_dsn")
    dsn = Path(res["dsn_path"])
    assert dsn.is_file()
    assert dsn.stat().st_size > 100

    # 2) Run Freerouting (low passes — no nets means it terminates fast anyway)
    route_res = freerouting.route(
        dsn, dsn.with_suffix(".ses"), passes=5, timeout_seconds=60,
    )
    assert route_res["returncode"] == 0
    ses = Path(route_res["ses_path"])
    assert ses.is_file()

    # 3) Import SES — should not error and the PCB should still be parseable.
    import_res = call("import_ses", ses_path=str(ses))
    assert Path(import_res["pcb_path"]).is_file()
    assert pcb_path.stat().st_size >= size_before_export
    state.clear_active()


@pytest.mark.slow
def test_autoroute_pcb_tool_end_to_end(tmp_path: Path):
    """Full single-tool autoroute flow."""
    ok, why = _have_prerequisites()
    if not ok:
        pytest.skip(why)

    state.clear_active()
    lib_tools._index = kicad_libs.load_cache()

    files = write_blank_project(tmp_path / "ar", "ar")
    state.set_active(tmp_path / "ar", "ar")

    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP("t")
    pcb_tools.register(mcp)
    routing_tools.register(mcp)

    def call(name, **kw):
        return mcp._tool_manager.get_tool(name).fn(**kw)

    call("set_board_outline", width_mm=50, height_mm=30)
    call("add_footprint", lib_id="Resistor_SMD:R_0603_1608Metric",
         reference="R1", value="10k", x_mm=20, y_mm=15)
    call("add_footprint", lib_id="Resistor_SMD:R_0603_1608Metric",
         reference="R2", value="1k", x_mm=35, y_mm=15)

    res = call("autoroute_pcb", passes=5, timeout_seconds=60)
    assert res["freerouting_returncode"] == 0
    assert Path(res["dsn_path"]).is_file()
    assert Path(res["ses_path"]).is_file()
    assert Path(res["pcb_path"]).is_file()
    state.clear_active()
