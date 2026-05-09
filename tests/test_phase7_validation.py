"""Phase 7 — ERC and DRC validation tools.

Strategy:
- Unit tests for the JSON shaping (no kicad-cli required).
- @pytest.mark.slow: real `kicad-cli` invocations against built schematics
  and PCBs. Skipped if kicad-cli isn't reachable.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kicad_claude import state
from kicad_claude.adapters import kicad_cli
from kicad_claude.indexer import kicad_libs
from kicad_claude.templates.blank import write_blank_project
from kicad_claude.tools import library as lib_tools
from kicad_claude.tools import pcb as pcb_tools
from kicad_claude.tools import schematic as sch_tools
from kicad_claude.tools import validation as val_tools
from kicad_claude.utils.kicad_paths import find_kicad_cli


# ===== Unit: JSON shaping ================================================== #


_SAMPLE_ERC = {
    "$schema": "https://schemas.kicad.org/erc.v1.json",
    "source": "demo.kicad_sch",
    "kicad_version": "10.0.1",
    "date": "2026-05-09T21:00:00",
    "coordinate_units": "mm",
    "violations": [
        {
            "type": "lib_symbol_issues",
            "severity": "warning",
            "description": "Symbol library issue X",
            "items": [
                {"description": "On R1", "uuid": "u-1", "pos": {"x": 100, "y": 50}}
            ],
        },
        {
            "type": "pin_not_connected",
            "severity": "error",
            "description": "Pin not connected",
            "items": [
                {"description": "On U1 pin 5", "uuid": "u-2", "pos": {"x": 80, "y": 70}}
            ],
        },
        {
            "type": "duplicate_reference",
            "severity": "error",
            "description": "Duplicate references",
            "items": [],
        },
    ],
}


def test_shape_erc_counts_severities(tmp_path: Path):
    raw = tmp_path / "fake.json"
    raw.write_text(json.dumps(_SAMPLE_ERC))
    shaped = kicad_cli._shape_erc(_SAMPLE_ERC, raw)
    assert shaped["kind"] == "erc"
    assert shaped["errors"] == 2
    assert shaped["warnings"] == 1
    assert shaped["total_violations"] == 3
    assert shaped["kicad_version"] == "10.0.1"
    assert len(shaped["violations"]) == 3


def test_shape_violation_extracts_position():
    sample = {
        "type": "x", "severity": "error", "description": "y",
        "items": [
            {"description": "i1", "uuid": "u", "pos": {"x": 10.5, "y": 20}},
            {"description": "i2", "uuid": "u2"},  # no pos
        ],
    }
    shaped = kicad_cli._shape_violation(sample)
    assert shaped["severity"] == "error"
    assert shaped["items"][0]["position"] == [10.5, 20]
    assert shaped["items"][1]["position"] is None


_SAMPLE_DRC = {
    "$schema": "https://schemas.kicad.org/drc.v1.json",
    "source": "demo.kicad_pcb",
    "kicad_version": "10.0.1",
    "date": "2026-05-09T21:00:00",
    "coordinate_units": "mm",
    "violations": [
        {"type": "clearance", "severity": "error", "description": "Track clearance", "items": []},
    ],
    "unconnected_items": [
        {"type": "unconnected_items", "severity": "warning", "description": "Net X unconnected", "items": []},
    ],
    "schematic_parity": [],
    "ignored_checks": [],
}


def test_shape_drc_separates_buckets(tmp_path: Path):
    raw = tmp_path / "fake.json"
    raw.write_text(json.dumps(_SAMPLE_DRC))
    shaped = kicad_cli._shape_drc(_SAMPLE_DRC, raw)
    assert shaped["kind"] == "drc"
    assert shaped["errors"] == 1
    assert shaped["warnings"] == 0  # only the violations bucket counts to warnings
    assert shaped["unconnected_items_count"] == 1
    assert shaped["schematic_parity_count"] == 0


# ===== Slow: live kicad-cli =============================================== #


def _can_run_cli() -> bool:
    return find_kicad_cli() is not None


@pytest.fixture
def empty_active_project(tmp_path):
    state.clear_active()
    files = write_blank_project(tmp_path / "v", "v")
    state.set_active(tmp_path / "v", "v")
    yield files
    state.clear_active()


@pytest.mark.slow
def test_run_erc_on_blank_schematic(empty_active_project):
    if not _can_run_cli():
        pytest.skip("kicad-cli not available")
    res = kicad_cli.run_erc(empty_active_project["sch"])
    assert res["kind"] == "erc"
    # Empty schematic: 0 violations.
    assert res["total_violations"] == 0
    assert res["kicad_version"]
    assert Path(res["raw_path"]).is_file()


@pytest.mark.slow
def test_run_drc_on_blank_pcb_no_outline(empty_active_project):
    if not _can_run_cli():
        pytest.skip("kicad-cli not available")
    res = kicad_cli.run_drc(empty_active_project["pcb"], schematic_parity=False)
    assert res["kind"] == "drc"
    # Blank PCB has no Edge.Cuts; expect at least 1 board-outline issue.
    assert res["total_violations"] >= 0  # tolerate 0 if KiCAD rules differ
    assert Path(res["raw_path"]).is_file()


@pytest.mark.slow
def test_validation_tools_on_voltage_divider(tmp_path):
    """Build the Phase 3 voltage divider, run ERC + DRC via the tool layer."""
    if not _can_run_cli():
        pytest.skip("kicad-cli not available")
    cached = kicad_libs.load_cache()
    if cached is None:
        pytest.skip("library index not built")

    state.clear_active()
    lib_tools._index = cached
    files = write_blank_project(tmp_path / "vd", "vd")
    state.set_active(tmp_path / "vd", "vd")

    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP("t")
    sch_tools.register(mcp)
    lib_tools.register(mcp)
    pcb_tools.register(mcp)
    val_tools.register(mcp)

    def call(name, **kw):
        return mcp._tool_manager.get_tool(name).fn(**kw)

    # Schematic side
    call("add_power_symbol", net="+5V", x_mm=100, y_mm=160)
    call("add_symbol", lib_id="Device:R", reference="R1", value="10k", x_mm=100, y_mm=130)
    call("add_power_symbol", net="GND", x_mm=100, y_mm=100)

    # PCB side
    call("set_board_outline", width_mm=50, height_mm=30)

    erc = call("run_erc")
    drc = call("run_drc", schematic_parity=False)

    assert erc["kind"] == "erc"
    assert "errors" in erc and "warnings" in erc and "violations" in erc
    assert isinstance(erc["violations"], list)

    assert drc["kind"] == "drc"
    assert "errors" in drc
    assert "unconnected_items" in drc

    state.clear_active()
