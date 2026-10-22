"""Phase 16 — net classes that KiCad 10 actually applies."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from mcp.server.fastmcp import FastMCP

from kicad_claude.adapters import project_settings as ps
from kicad_claude.tools import rules as rules_tools

from .carrier_support import _run_cli, call, needs_kicad


def test_ensure_current_net_settings_upgrades_legacy_block():
    pro = {"net_settings": {"classes": [], "meta": {"version": 0}}}
    ps.ensure_current_net_settings(pro)
    settings = pro["net_settings"]
    assert settings["meta"]["version"] == ps.NET_SETTINGS_VERSION
    assert settings["classes"][0]["name"] == "Default"
    assert settings["netclass_patterns"] == []


def test_ensure_current_net_settings_is_idempotent_and_keeps_classes():
    pro = {}
    ps.ensure_current_net_settings(pro)
    ps.add_or_update_net_class(pro, "SE50", track_width_mm=0.1)
    again = json.dumps(ps.ensure_current_net_settings(pro), sort_keys=True)
    assert json.dumps(ps.ensure_current_net_settings(pro), sort_keys=True) == again
    assert [c["name"] for c in pro["net_settings"]["classes"]] == ["Default", "SE50"]


def test_new_project_starts_with_current_net_settings(blank_project):
    pro = json.loads(blank_project["pro"].read_text())
    assert pro["net_settings"]["meta"]["version"] >= 4
    assert pro["net_settings"]["classes"][0]["name"] == "Default"


@pytest.mark.parametrize("given,expected", [
    ("CLK_*", ["CLK_*", "*/CLK_*"]),
    ("*CLK_*", ["*CLK_*"]),
    ("/CLK_P", ["/CLK_P"]),
    ("+5V", ["+5V", "*/+5V"]),
])
def test_qualified_patterns(given, expected):
    assert ps.qualified_patterns(given) == expected


@pytest.fixture
def rules_mcp(blank_project):
    server = FastMCP("rules")
    rules_tools.register(server)
    return server


def test_assign_net_class_stores_both_spellings(rules_mcp, blank_project):
    call(rules_mcp, "add_net_class", name="SE50", track_width_mm=0.1)
    res = call(rules_mcp, "assign_net_class", net_pattern="DDR_*", class_name="SE50")
    assert [a["pattern"] for a in res["assignments"]] == ["DDR_*", "*/DDR_*"]


def test_legacy_project_is_repaired_on_first_class_edit(rules_mcp, blank_project):
    pro = json.loads(blank_project["pro"].read_text())
    pro["net_settings"] = {"classes": [], "meta": {"version": 0}}
    blank_project["pro"].write_text(json.dumps(pro))
    call(rules_mcp, "add_net_class", name="SE50", track_width_mm=0.1)
    repaired = json.loads(blank_project["pro"].read_text())["net_settings"]
    assert repaired["meta"]["version"] >= 4


@needs_kicad
def test_kicad_applies_local_and_global_net_classes(mcp, rules_mcp, blank_project, tmp_path):
    """Root-sheet and global nets. Nets that live only on a sub-sheet showed
    class Default in kicad-cli's netlist even with matching patterns (KiCad
    10.0.1); use global labels for nets that need a class."""
    call(rules_mcp, "add_net_class", name="SE50", track_width_mm=0.1)
    call(rules_mcp, "add_diff_pair_class", name="DIFF90", diff_pair_width_mm=0.1,
         diff_pair_gap_mm=0.15)
    call(rules_mcp, "assign_net_class", net_pattern="DDR_*", class_name="SE50")
    call(rules_mcp, "assign_net_class", net_pattern="CLK_*", class_name="DIFF90")
    call(mcp, "add_symbol", lib_id="MiniLib:Resistor", reference="R1", value="1k",
         x_mm=101.6, y_mm=101.6)
    call(mcp, "connect_pins", reference="R1", connections=[
        {"pin": "1", "net": "CLK_P"}, {"pin": "2", "net": "DDR_DQ0", "scope": "global"}])
    call(mcp, "add_symbol", lib_id="MiniLib:Resistor", reference="R2", value="1k",
         x_mm=152.4, y_mm=101.6)
    call(mcp, "connect_pins", reference="R2", connections=[
        {"pin": "1", "net": "CLK_N", "scope": "global"}, {"pin": "2", "net": "OTHER"}])
    out = tmp_path / "n.xml"
    _run_cli("sch", "export", "netlist", "--format", "kicadxml", "-o", str(out),
             str(blank_project["sch"]))
    import re
    classes = dict(re.findall(r'<net code="\d+" name="([^"]+)" class="([^"]+)"', out.read_text()))
    assert classes == {"/CLK_P": "DIFF90", "DDR_DQ0": "SE50",
                       "CLK_N": "DIFF90", "/OTHER": "Default"}
