"""Phase 10 — design rules, net classes, annotation, schematic↔PCB sync.

Strategy:
- Unit tests on pure-Python helpers (project_settings, annotation).
- @pytest.mark.slow: real kicad-cli + pcbnew Python pipeline:
    1) divider with R? refs auto-annotates to R1/R2
    2) JLCPCB preset applied → DRC respects it
    3) update_pcb_from_schematic propagates nets
    4) autoroute_pcb actually produces track segments
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kicad_claude import state
from kicad_claude.adapters import (
    annotation,
    project_settings as ps,
    sch_editor as sch_ed,
    sch_io,
)
from kicad_claude.indexer import kicad_libs
from kicad_claude.templates.blank import write_blank_project
from kicad_claude.tools import library as lib_tools


# ===== Project settings (no kicad-cli) ===================================== #


def test_load_save_pro_round_trip(tmp_path: Path):
    files = write_blank_project(tmp_path / "p", "p")
    pro = ps.load_pro(files["pro"])
    pro["meta"]["filename"] = "p.kicad_pro"
    ps.save_pro(files["pro"], pro)
    re_loaded = ps.load_pro(files["pro"])
    assert re_loaded["meta"]["filename"] == "p.kicad_pro"


def test_update_design_rules_filters_none(tmp_path: Path):
    pro = {"board": {"design_settings": {"rules": {}}}}
    ps.update_design_rules(
        pro,
        min_clearance=0.127,
        min_track_width=None,  # ignored
        min_via_drill=0.2,
    )
    rules = ps.get_design_rules(pro)
    assert rules == {"min_clearance": 0.127, "min_via_drill": 0.2}


def test_fab_presets_have_rules_and_descriptions():
    for name, preset in ps.FAB_PRESETS.items():
        assert "rules" in preset
        assert "description" in preset
        assert isinstance(preset["rules"], dict)
        assert preset["rules"]  # not empty


def test_add_or_update_net_class_creates_then_updates(tmp_path: Path):
    pro = {}
    cls = ps.add_or_update_net_class(
        pro, "Power", track_width_mm=0.5, clearance_mm=0.25,
    )
    assert cls["name"] == "Power"
    assert cls["track_width"] == 0.5
    # Update keeps name, modifies fields
    cls2 = ps.add_or_update_net_class(pro, "Power", track_width_mm=0.6)
    assert cls2["track_width"] == 0.6
    assert len(ps.get_net_classes(pro)) == 1


def test_assign_pattern_requires_existing_class(tmp_path: Path):
    pro = {}
    with pytest.raises(KeyError, match="doesn't exist"):
        ps.assign_pattern(pro, netclass="Bogus", pattern="+5V")
    ps.add_or_update_net_class(pro, "Power")
    entry = ps.assign_pattern(pro, netclass="Power", pattern="+5V")
    assert entry["pattern"] == "+5V"
    # Idempotent
    again = ps.assign_pattern(pro, netclass="Power", pattern="+5V")
    assert again == entry
    assert len(ps.get_netclass_patterns(pro)) == 1


def test_remove_net_class_drops_dangling_patterns(tmp_path: Path):
    pro = {}
    ps.add_or_update_net_class(pro, "Power")
    ps.assign_pattern(pro, netclass="Power", pattern="+5V")
    assert len(ps.get_netclass_patterns(pro)) == 1
    assert ps.remove_net_class(pro, "Power") is True
    assert len(ps.get_netclass_patterns(pro)) == 0


# ===== Annotation (no kicad-cli) =========================================== #


def _build_sch_with_unannotated_resistors(tmp_path: Path):
    """Create a schematic with R?, R?, R? at increasing y-positions."""
    state.clear_active()
    files = write_blank_project(tmp_path / "p", "p")
    state.set_active(tmp_path / "p", "p")

    fixtures = Path(__file__).parent / "fixtures"
    sym_def = sch_ed.fetch_symbol_def(fixtures / "MiniLib.kicad_sym", "Resistor")
    tree = sch_io.parse_file(files["sch"])
    for i, y in enumerate((140, 100, 60), start=1):
        defn = sch_ed.fetch_symbol_def(fixtures / "MiniLib.kicad_sym", "Resistor")
        sch_ed.add_symbol(
            tree,
            qualified_lib_id="MiniLib:Resistor",
            reference="R?",
            value=f"{i}k",
            x_mm=100, y_mm=y, rotation=0,
            sym_def_node=defn,
            project_name="p",
        )
    sch_io.write_file(files["sch"], tree)
    return files


def test_annotate_tree_assigns_sequential_numbers(tmp_path: Path):
    files = _build_sch_with_unannotated_resistors(tmp_path)
    tree = sch_io.parse_file(files["sch"])
    assignments = annotation.annotate_tree(tree)
    assert len(assignments) == 3
    new_refs = sorted(a["new"] for a in assignments)
    assert new_refs == ["R1", "R2", "R3"]
    state.clear_active()


def test_annotate_tree_continues_after_existing(tmp_path: Path):
    """If R5 is already used, the next unannotated becomes R6."""
    state.clear_active()
    files = write_blank_project(tmp_path / "q", "q")
    state.set_active(tmp_path / "q", "q")
    fixtures = Path(__file__).parent / "fixtures"
    tree = sch_io.parse_file(files["sch"])
    for ref, y in (("R5", 140), ("R?", 100)):
        defn = sch_ed.fetch_symbol_def(fixtures / "MiniLib.kicad_sym", "Resistor")
        sch_ed.add_symbol(
            tree, qualified_lib_id="MiniLib:Resistor",
            reference=ref, value="1k", x_mm=100, y_mm=y, rotation=0,
            sym_def_node=defn, project_name="q",
        )
    sch_io.write_file(files["sch"], tree)
    tree = sch_io.parse_file(files["sch"])
    assignments = annotation.annotate_tree(tree)
    assert assignments[0]["new"] == "R6"
    state.clear_active()


def test_annotate_does_not_touch_already_annotated(tmp_path: Path):
    files = _build_sch_with_unannotated_resistors(tmp_path)
    tree = sch_io.parse_file(files["sch"])
    annotation.annotate_tree(tree)
    # Re-running shouldn't re-assign
    again = annotation.annotate_tree(tree)
    assert again == []
    state.clear_active()


def test_existing_max_per_prefix(tmp_path: Path):
    state.clear_active()
    files = write_blank_project(tmp_path / "x", "x")
    state.set_active(tmp_path / "x", "x")
    fixtures = Path(__file__).parent / "fixtures"
    tree = sch_io.parse_file(files["sch"])
    for ref in ("R1", "R3", "C2", "U1"):
        defn = sch_ed.fetch_symbol_def(fixtures / "MiniLib.kicad_sym", "Resistor")
        sch_ed.add_symbol(
            tree, qualified_lib_id="MiniLib:Resistor",
            reference=ref, value="x", x_mm=10, y_mm=10, rotation=0,
            sym_def_node=defn, project_name="x",
        )
    counts = annotation.existing_max_per_prefix(tree)
    assert counts == {"R": 3, "C": 2, "U": 1}
    state.clear_active()


# ===== Tool layer ========================================================== #


def _make_mcp(monkeypatch, tmp_path):
    from mcp.server.fastmcp import FastMCP
    fixtures = Path(__file__).parent / "fixtures"
    import shutil
    sym_dir = tmp_path / "syms"
    sym_dir.mkdir()
    shutil.copy(fixtures / "MiniLib.kicad_sym", sym_dir / "MiniLib.kicad_sym")
    idx = kicad_libs.build_index(symbol_dirs=[sym_dir], footprint_dirs=[])
    monkeypatch.setattr(lib_tools, "load_cache", lambda: idx)
    monkeypatch.setattr(lib_tools, "_index", None)

    mcp = FastMCP("t")
    from kicad_claude.tools import schematic as sch_tools
    from kicad_claude.tools import rules as rules_tools
    from kicad_claude.tools import sync as sync_tools
    sch_tools.register(mcp)
    lib_tools.register(mcp)
    rules_tools.register(mcp)
    sync_tools.register(mcp)
    return mcp


def _call(mcp, _tool_name, **kw):
    return mcp._tool_manager.get_tool(_tool_name).fn(**kw)


def test_set_design_rules_writes_to_kicad_pro(tmp_path: Path, monkeypatch):
    state.clear_active()
    files = write_blank_project(tmp_path / "p", "p")
    state.set_active(tmp_path / "p", "p")

    mcp = _make_mcp(monkeypatch, tmp_path)
    res = _call(mcp, "set_design_rules",
                min_clearance_mm=0.127, min_track_width_mm=0.127)
    assert res["rules"]["min_clearance"] == 0.127
    pro = json.loads(files["pro"].read_text())
    assert pro["board"]["design_settings"]["rules"]["min_clearance"] == 0.127
    state.clear_active()


def test_apply_fab_preset_writes_full_ruleset(tmp_path: Path, monkeypatch):
    state.clear_active()
    files = write_blank_project(tmp_path / "p", "p")
    state.set_active(tmp_path / "p", "p")
    mcp = _make_mcp(monkeypatch, tmp_path)
    res = _call(mcp, "apply_fab_preset", preset="jlcpcb_2l_default")
    assert res["preset"] == "jlcpcb_2l_default"
    pro = json.loads(files["pro"].read_text())
    rules = pro["board"]["design_settings"]["rules"]
    assert rules["min_clearance"] == 0.127
    assert rules["min_track_width"] == 0.127
    assert rules["allow_microvias"] is False
    state.clear_active()


def test_apply_fab_preset_unknown_raises(tmp_path: Path, monkeypatch):
    state.clear_active()
    files = write_blank_project(tmp_path / "p", "p")
    state.set_active(tmp_path / "p", "p")
    mcp = _make_mcp(monkeypatch, tmp_path)
    with pytest.raises(KeyError):
        _call(mcp, "apply_fab_preset", preset="not_a_real_fab")
    state.clear_active()


def test_add_and_assign_net_class(tmp_path: Path, monkeypatch):
    state.clear_active()
    files = write_blank_project(tmp_path / "p", "p")
    state.set_active(tmp_path / "p", "p")
    mcp = _make_mcp(monkeypatch, tmp_path)
    _call(mcp, "add_net_class", name="Power",
          track_width_mm=0.5, clearance_mm=0.25)
    _call(mcp, "assign_net_class", net_pattern="+5V", class_name="Power")
    _call(mcp, "assign_net_class", net_pattern="GND", class_name="Power")
    listed = _call(mcp, "list_net_classes")
    power = next(c for c in listed["classes"] if c["name"] == "Power")
    assert power["track_width"] == 0.5
    assert sorted(power["patterns"]) == ["+5V", "GND"]
    state.clear_active()


def test_annotate_schematic_tool(tmp_path: Path, monkeypatch):
    files = _build_sch_with_unannotated_resistors(tmp_path)
    mcp = _make_mcp(monkeypatch, tmp_path)
    res = _call(mcp, "annotate_schematic")
    assert res["total_assignments"] == 3
    assigned = [a for s in res["sheets"] for a in s["assignments"]]
    assert sorted(a["new"] for a in assigned) == ["R1", "R2", "R3"]
    state.clear_active()


# ===== Slow acceptance =============================================== #


@pytest.mark.slow
def test_acceptance_full_flow_routes_real_track(tmp_path: Path):
    """Divider → annotate → apply rules → update PCB → autoroute → segments exist."""
    from kicad_claude.utils.kicad_paths import find_kicad_cli
    if find_kicad_cli() is None:
        pytest.skip("kicad-cli not available")
    cached = kicad_libs.load_cache()
    if cached is None:
        pytest.skip("library index not built")

    state.clear_active()
    lib_tools._index = cached

    files = write_blank_project(tmp_path / "div", "div")
    state.set_active(tmp_path / "div", "div")

    from mcp.server.fastmcp import FastMCP
    from kicad_claude.tools import (
        library, pcb, routing, rules, schematic, sync, validation,
    )
    mcp = FastMCP("t")
    for mod in (library, pcb, routing, rules, schematic, sync, validation):
        mod.register(mcp)

    def call(name, **kw):
        return mcp._tool_manager.get_tool(name).fn(**kw)

    # Schematic
    call("add_power_symbol", net="+5V", x_mm=100, y_mm=160)
    call("add_symbol", lib_id="Device:R", reference="R?", value="10k",
         x_mm=100, y_mm=130)
    call("add_symbol", lib_id="Device:R", reference="R?", value="1k",
         x_mm=100, y_mm=80)
    call("add_power_symbol", net="GND", x_mm=100, y_mm=40)
    call("annotate_schematic")
    # Wire using pin positions so the netlist resolves cleanly
    r1p1 = call("get_pin_position", reference="R1", pin="1")["position_mm"]
    r1p2 = call("get_pin_position", reference="R1", pin="2")["position_mm"]
    r2p1 = call("get_pin_position", reference="R2", pin="1")["position_mm"]
    r2p2 = call("get_pin_position", reference="R2", pin="2")["position_mm"]
    call("add_wire", x1_mm=100, y1_mm=160, x2_mm=r1p1[0], y2_mm=r1p1[1])
    call("add_wire", x1_mm=r1p2[0], y1_mm=r1p2[1], x2_mm=r2p1[0], y2_mm=r2p1[1])
    call("add_wire", x1_mm=r2p2[0], y1_mm=r2p2[1], x2_mm=100, y2_mm=40)

    # PCB
    call("set_board_outline", width_mm=50, height_mm=30)
    call("apply_fab_preset", preset="jlcpcb_2l_default")
    call("add_footprint", lib_id="Resistor_SMD:R_0603_1608Metric",
         reference="R1", value="10k", x_mm=20, y_mm=15)
    call("add_footprint", lib_id="Resistor_SMD:R_0603_1608Metric",
         reference="R2", value="1k", x_mm=35, y_mm=15)

    upd = call("update_pcb_from_schematic")
    assert upd["matched_footprints"] == 2
    assert upd["pad_assignments_made"] == 4

    ar = call("autoroute_pcb", passes=20, timeout_seconds=60)
    assert ar["freerouting_returncode"] == 0

    import sexpdata
    from kicad_claude.adapters.sch_io import find_children
    pcb_tree = sexpdata.loads(files["pcb"].read_text())
    segments = find_children(pcb_tree, "segment")
    # Expect at least one routed segment (R1.pad2 ↔ R2.pad1 mid-node)
    assert len(segments) >= 1, "Freerouting did not produce any track segments"

    state.clear_active()
