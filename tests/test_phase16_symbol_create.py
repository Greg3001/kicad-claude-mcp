"""Phase 16 — create_symbol: multi-unit symbols with automatic pin layout."""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from kicad_claude.adapters import library_create as lc
from kicad_claude.adapters import sch_editor as ed
from kicad_claude.adapters import sch_io, symbol_layout, symbol_lib

from .carrier_support import call, erc_counts, needs_kicad, netlist_pins


def pins_for(prefix: str, count: int, unit: int = 1, **extra) -> list[dict]:
    return [
        {"number": f"{prefix}{i}", "name": f"{prefix}_SIG{i}", "unit": unit, **extra}
        for i in range(1, count + 1)
    ]


def build(pins: list[dict]) -> list:
    return lc.build_symbol_node(qualified_lib_id="lib:Part", pins=pins)


def unit_nodes(node: list) -> dict[str, list]:
    return {c[1]: c for c in node[2:] if sch_io.is_call(c, "symbol")}


def on_grid(v: float, grid: float = 1.27) -> bool:
    return math.isclose(v / grid, round(v / grid), abs_tol=1e-6)


# ===== automatic layout ==================================================== #


def test_auto_layout_splits_pins_over_both_sides():
    node = build(pins_for("A", 10))
    pins = sch_io.find_children(unit_nodes(node)["Part_1_1"], "pin")
    xs = [sch_io.find_child(p, "at")[1] for p in pins]
    assert sum(x < 0 for x in xs) == 5 and sum(x > 0 for x in xs) == 5


def test_auto_layout_pins_point_into_the_body():
    node = build(pins_for("A", 4))
    for pin in sch_io.find_children(unit_nodes(node)["Part_1_1"], "pin"):
        _, x, _, angle = sch_io.find_child(pin, "at")
        assert angle == (0 if x < 0 else 180)


def test_auto_layout_tips_on_grid_and_rows_at_pitch():
    layout = symbol_layout.layout_unit([{"number": str(i), "name": "X"} for i in range(7)])
    left = sorted((p.y_mm for p in layout.pins if p.x_mm < 0), reverse=True)
    assert all(on_grid(p.x_mm) and on_grid(p.y_mm) for p in layout.pins)
    assert all(math.isclose(a - b, 2.54) for a, b in zip(left, left[1:]))
    assert max(p.y_mm for p in layout.pins) < layout.half_height_mm


def test_auto_layout_body_widens_for_long_names():
    short = symbol_layout.layout_unit([{"number": "1", "name": "A"}, {"number": "2", "name": "B"}])
    long = symbol_layout.layout_unit(
        [{"number": "1", "name": "A" * 20}, {"number": "2", "name": "B" * 20}]
    )
    assert long.half_width_mm > short.half_width_mm


def test_side_is_honoured():
    pins = [
        {"number": "1", "name": "L", "side": "left"},
        {"number": "2", "name": "R1", "side": "right"},
        {"number": "3", "name": "R2", "side": "right"},
    ]
    layout = symbol_layout.layout_unit(pins)
    side = {p.spec["number"]: "left" if p.x_mm < 0 else "right" for p in layout.pins}
    assert side == {"1": "left", "2": "right", "3": "right"}


def test_unknown_side_rejected():
    with pytest.raises(ValueError, match="side must be"):
        symbol_layout.layout_unit([{"number": "1", "side": "top"}])


# ===== units =============================================================== #


def test_pins_group_into_units():
    node = build(pins_for("A", 3, unit=1) + pins_for("B", 2, unit=2) + pins_for("C", 4, unit=3))
    subs = unit_nodes(node)
    assert list(subs) == ["Part_1_1", "Part_2_1", "Part_3_1"]
    assert [len(sch_io.find_children(subs[k], "pin")) for k in subs] == [3, 2, 4]


def test_unit_numbers_come_back_from_the_built_symbol():
    node = build(pins_for("A", 2, unit=1) + pins_for("B", 2, unit=2))
    assert symbol_lib.unit_numbers(node) == [1, 2]


def test_duplicate_pin_number_rejected_even_across_units():
    pins = pins_for("A", 2, unit=1) + [{"number": "A1", "name": "dup", "unit": 2}]
    with pytest.raises(ValueError, match="duplicate pin number"):
        build(pins)


def test_mixed_explicit_and_auto_in_one_unit_rejected():
    pins = [{"number": "1", "x_mm": -5, "y_mm": 0}, {"number": "2", "name": "auto"}]
    with pytest.raises(ValueError, match="every pin or for none"):
        build(pins)


def test_explicit_layout_still_honoured():
    pins = [{"number": "1", "name": "A", "x_mm": -7.62, "y_mm": 1.27, "angle_deg": 0}]
    node = build(pins)
    pin = sch_io.find_child(unit_nodes(node)["Part_1_1"], "pin")
    assert sch_io.find_child(pin, "at")[1:] == [-7.62, 1.27, 0]


def test_replace_overwrites_instead_of_raising(tmp_path: Path):
    lib = tmp_path / "lib" / "L.kicad_sym"
    lc.append_symbol_to_lib(lib, build(pins_for("A", 2)))
    with pytest.raises(FileExistsError):
        lc.append_symbol_to_lib(lib, build(pins_for("A", 3)))
    lc.append_symbol_to_lib(lib, build(pins_for("A", 3)), replace=True)
    tree = sch_io.parse_file(lib)
    parts = [c for c in tree[1:] if sch_io.is_call(c, "symbol")]
    assert len(parts) == 1
    assert len(ed.collect_pin_numbers(parts[0])) == 3


# ===== 4-unit, 480-pin connector through the tool and into KiCad ============ #


def som_connector_pins() -> list[dict]:
    pins = []
    for unit in range(1, 5):
        pins += pins_for(f"U{unit}P", 120, unit=unit, type="passive")
    return pins


def test_create_symbol_tool_reports_units(mcp, blank_project):
    res = call(mcp, "create_symbol", lib_name="som", symbol_name="K26_CONN",
               pins=som_connector_pins(), reference_prefix="J")
    assert res["pin_count"] == 480
    assert res["units"] == [1, 2, 3, 4]


@needs_kicad
def test_480_pin_four_unit_symbol_loads_in_kicad(mcp, blank_project):
    call(mcp, "create_symbol", lib_name="som", symbol_name="K26_CONN",
         pins=som_connector_pins(), reference_prefix="J")
    call(mcp, "set_sheet_paper", paper="A3")  # 60-row units do not fit on A4
    for unit in range(1, 5):
        call(mcp, "add_symbol", lib_id="som:K26_CONN", reference="J1", value="K26",
             x_mm=76.2 + (unit - 1) * 76.2, y_mm=147.32, unit=unit)
    # The netlist, not ERC, is the exact pin census: KiCad 10.0.1's ERC skips
    # a few percent of floating pins in a dense symbol (position-dependent).
    nets = netlist_pins(blank_project["sch"])
    assert sum(len(n) for n in nets.values()) == 480
    counts = erc_counts(blank_project["sch"])
    assert counts["missing_unit"] == 0
    assert counts["lib_symbol_mismatch"] == 0


def test_generated_symbol_has_empty_datasheet_not_tilde(tmp_path: Path):
    node = lc.build_symbol_node(qualified_lib_id="lib:Part", pins=pins_for("A", 2))
    assert ed.get_symbol_property(node, "Datasheet") == ""


@needs_kicad
def test_generated_symbol_matches_library_copy_in_kicad(mcp, blank_project):
    """KiCad 10 reads a literal "~" datasheet as a diff against the embedded copy."""
    call(mcp, "create_symbol", lib_name="som", symbol_name="K", pins=pins_for("A", 4))
    call(mcp, "add_symbol", lib_id="som:K", reference="J1", value="K", x_mm=76.2, y_mm=101.6)
    assert erc_counts(blank_project["sch"])["lib_symbol_mismatch"] == 0


# ===== KiCad 10 library format =============================================== #


def test_generated_symbol_uses_kicad10_flags():
    node = build(pins_for("A", 2))
    assert sch_io.find_child(node, "in_pos_files")[1] == sch_io.sym("yes")
    assert sch_io.find_child(node, "duplicate_pin_numbers_are_jumpers")[1] == sch_io.sym("no")
    footprint = [p for p in sch_io.find_children(node, "property") if p[1] == "Footprint"][0]
    assert sch_io.find_child(footprint, "hide") is not None  # on the property, not in effects
    assert sch_io.find_child(sch_io.find_child(footprint, "effects"), "hide") is None
    assert sch_io.find_child(footprint, "show_name") is not None


@needs_kicad
def test_lib_survives_kicad_upgrade_unchanged_in_meaning(mcp, blank_project):
    """A hand-written old-style library (effects-level hide, "~" datasheet) must not
    trip lib_symbol_mismatch now that new sheets carry KiCad 10's format version."""
    lib = blank_project["sch"].parent / "lib" / "old.kicad_sym"
    lib.parent.mkdir(exist_ok=True)
    lib.write_text("""(kicad_symbol_lib (version 20251024) (generator "x")
(symbol "OLD"
  (property "Reference" "U" (at 0 0 0) (effects (font (size 1.27 1.27))))
  (property "Value" "OLD" (at 0 0 0) (effects (font (size 1.27 1.27))))
  (property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
  (property "Datasheet" "~" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
  (property "Description" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
  (symbol "OLD_1_1"
    (pin passive line (at -7.62 0 0) (length 2.54)
      (name "A" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
  )))""")
    from kicad_claude.adapters import vendor_import

    vendor_import.update_sym_lib_table(blank_project["sch"].parent, "old")
    call(mcp, "add_symbol", lib_id="old:OLD", reference="U1", value="OLD", x_mm=101.6, y_mm=101.6)
    assert erc_counts(blank_project["sch"])["lib_symbol_mismatch"] == 0


# ===== every KiCad electrical pin type ======================================= #

ALL_PIN_TYPES = ["input", "output", "bidirectional", "tri_state", "passive", "free", "unspecified",
                 "power_in", "power_out", "open_collector", "open_emitter", "no_connect"]


def test_supported_pin_types_are_exactly_kicads():
    assert set(lc.PIN_TYPES) == set(ALL_PIN_TYPES)


@needs_kicad
def test_kicad_loads_a_symbol_using_every_pin_type(mcp, blank_project):
    pins = [{"number": str(i + 1), "name": t.upper(), "type": t} for i, t in enumerate(ALL_PIN_TYPES)]
    call(mcp, "create_symbol", lib_name="t", symbol_name="ALLTYPES", pins=pins)
    call(mcp, "add_symbol", lib_id="t:ALLTYPES", reference="U1", value="X", x_mm=101.6, y_mm=101.6)
    nets = netlist_pins(blank_project["sch"])
    assert sum(len(n) for n in nets.values()) == len(ALL_PIN_TYPES)
    assert erc_counts(blank_project["sch"])["lib_symbol_mismatch"] == 0


@needs_kicad
def test_tri_state_outputs_may_share_a_net_but_plain_outputs_may_not(mcp, blank_project):
    for kind in ("tri_state", "output"):
        for ref, x in (("U1", 76.2), ("U2", 127.0)):
            name = f"{kind}_{ref}"
            call(mcp, "create_symbol", lib_name="t", symbol_name=name,
                 pins=[{"number": "1", "name": "SO", "type": kind}])
            call(mcp, "add_symbol", lib_id=f"t:{name}", reference=f"{kind[0].upper()}{ref}", value="X",
                 x_mm=x, y_mm=101.6 if kind == "tri_state" else 152.4)
    for ref in ("TU1", "TU2", "OU1", "OU2"):
        call(mcp, "connect_pins", reference=ref, scope="global",
             connections=[{"pin": "1", "net": "MISO" if ref[0] == "T" else "MISO_PLAIN"}])
    counts = erc_counts(blank_project["sch"])
    assert counts["pin_to_pin"] == 1  # only the plain-output net conflicts
