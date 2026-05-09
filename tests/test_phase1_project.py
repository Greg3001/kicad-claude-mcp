"""Phase 1 — project management tools."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from skip import PCB, Schematic

from kicad_claude import state
from kicad_claude.templates.blank import write_blank_project
from kicad_claude.tools.project import register


@pytest.fixture(autouse=True)
def _reset_state():
    state.clear_active()
    yield
    state.clear_active()


def test_write_blank_project_creates_three_files(tmp_path: Path):
    files = write_blank_project(tmp_path / "demo", "demo")
    assert files["pro"].is_file()
    assert files["sch"].is_file()
    assert files["pcb"].is_file()
    assert files["pro"].name == "demo.kicad_pro"
    assert files["sch"].name == "demo.kicad_sch"
    assert files["pcb"].name == "demo.kicad_pcb"


def test_blank_pro_is_valid_json_with_correct_filename(tmp_path: Path):
    files = write_blank_project(tmp_path, "myboard")
    data = json.loads(files["pro"].read_text())
    assert data["meta"]["filename"] == "myboard.kicad_pro"
    assert data["meta"]["version"] == 3


def test_blank_sch_loads_with_kicad_skip(tmp_path: Path):
    files = write_blank_project(tmp_path, "p")
    sch = Schematic(str(files["sch"]))
    assert len(sch.symbol) == 0
    assert sch.uuid.value  # has a uuid


def test_blank_pcb_loads_with_kicad_skip(tmp_path: Path):
    files = write_blank_project(tmp_path, "p")
    pcb = PCB(str(files["pcb"]))
    assert len(getattr(pcb, "footprint", [])) == 0
    assert len(pcb.net) == 1  # default unconnected net


def test_two_calls_generate_unique_sch_uuids(tmp_path: Path):
    a = write_blank_project(tmp_path / "a", "a")
    b = write_blank_project(tmp_path / "b", "b")
    ua = Schematic(str(a["sch"])).uuid.value
    ub = Schematic(str(b["sch"])).uuid.value
    assert ua != ub


def test_refuses_overwrite(tmp_path: Path):
    write_blank_project(tmp_path, "p")
    with pytest.raises(FileExistsError):
        write_blank_project(tmp_path, "p")


def _make_mcp():
    """Build a minimal mcp-like collector to inspect registered tools."""
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("test")
    register(mcp)
    return mcp


def _call(mcp, tool_name, **kwargs):
    """Call a registered tool by invoking its underlying fn directly.

    FastMCP stores tools internally; we reach the python callable for unit tests.
    """
    tool = mcp._tool_manager.get_tool(tool_name)
    return tool.fn(**kwargs)


def test_create_project_tool_then_state(tmp_path: Path):
    mcp = _make_mcp()
    result = _call(mcp, "create_project", path=str(tmp_path / "blinky"), name="blinky")
    assert result["active"] is True
    assert result["symbols"] == 0
    assert result["footprints"] == 0
    assert result["nets"] == 1

    proj_state = _call(mcp, "get_project_state")
    assert proj_state["name"] == "blinky"
    assert proj_state["symbols"] == 0


def test_set_project_via_directory(tmp_path: Path):
    mcp = _make_mcp()
    _call(mcp, "create_project", path=str(tmp_path / "x"), name="x")
    state.clear_active()  # simulate fresh server session
    result = _call(mcp, "set_project", project_path=str(tmp_path / "x"))
    assert result["name"] == "x"
    assert result["active"] is True


def test_set_project_via_pro_file(tmp_path: Path):
    mcp = _make_mcp()
    _call(mcp, "create_project", path=str(tmp_path / "y"), name="y")
    state.clear_active()
    result = _call(mcp, "set_project", project_path=str(tmp_path / "y" / "y.kicad_pro"))
    assert result["name"] == "y"


def test_list_components_empty(tmp_path: Path):
    mcp = _make_mcp()
    _call(mcp, "create_project", path=str(tmp_path / "e"), name="e")
    assert _call(mcp, "list_components") == []


def test_get_project_state_without_active_raises(tmp_path: Path):
    mcp = _make_mcp()
    with pytest.raises(state.NoActiveProjectError):
        _call(mcp, "get_project_state")


def test_set_project_missing_pro_raises(tmp_path: Path):
    mcp = _make_mcp()
    with pytest.raises(FileNotFoundError):
        _call(mcp, "set_project", project_path=str(tmp_path))
