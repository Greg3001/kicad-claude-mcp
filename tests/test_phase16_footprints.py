"""Phase 16 - package pad generators and footprint validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_claude.adapters import footprint_pads as fp
from kicad_claude.adapters import kicad_cli, sch_io
from kicad_claude.adapters import library_create as lc

from .carrier_support import call, needs_kicad


def by_number(pads):
    return {p["number"]: p for p in pads}


# ===== dual row ============================================================== #


def test_dual_row_pin_one_is_top_left_and_numbering_goes_counter_clockwise():
    pads = by_number(fp.dual_row_pads(8, 1.27, 5.4, 1.6, 0.6))
    assert (pads["1"]["x_mm"], pads["1"]["y_mm"]) == (-2.7, -1.905)
    assert (pads["4"]["x_mm"], pads["4"]["y_mm"]) == (-2.7, 1.905)   # bottom of left column
    assert (pads["5"]["x_mm"], pads["5"]["y_mm"]) == (2.7, 1.905)    # bottom of right column
    assert (pads["8"]["x_mm"], pads["8"]["y_mm"]) == (2.7, -1.905)   # top of right column


def test_dual_row_pads_are_oriented_across_the_rows():
    pad = fp.dual_row_pads(4, 0.65, 5.0, 1.5, 0.4)[0]
    assert (pad["size_x_mm"], pad["size_y_mm"]) == (1.5, 0.4)


def test_dual_row_exposed_pad_is_centred_and_numbered():
    pads = fp.dual_row_pads(44, 0.635, 7.0, 1.5, 0.35, ep_width_mm=4.31, ep_height_mm=8.26)
    assert len(pads) == 45
    ep = pads[-1]
    assert (ep["number"], ep["x_mm"], ep["y_mm"], ep["size_x_mm"], ep["size_y_mm"]) == ("45", 0, 0, 4.31, 8.26)
    assert fp.dual_row_pads(8, 1.27, 5.4, 1.6, 0.6, ep_width_mm=2, ep_height_mm=2, ep_number="EP")[-1]["number"] == "EP"


@pytest.mark.parametrize("kwargs", [
    {"pin_count": 7}, {"pin_count": 0}, {"pin_count": 402}, {"pitch_mm": 0}, {"row_span_mm": -1},
    {"ep_width_mm": 2.0},  # exposed pad needs both sides
])
def test_dual_row_rejects_bad_input(kwargs):
    args = {"pin_count": 8, "pitch_mm": 1.27, "row_span_mm": 5.4, "pad_length_mm": 1.6, "pad_width_mm": 0.6}
    args.update(kwargs)
    with pytest.raises(ValueError):
        fp.dual_row_pads(**args)


# ===== QFN ==================================================================== #


def test_qfn_numbering_walks_the_four_sides_counter_clockwise():
    pads = by_number(fp.qfn_pads(3, 0.5, 6.0, 0.8, 0.25))
    assert len(pads) == 12
    assert (pads["1"]["x_mm"], pads["1"]["y_mm"]) == (-3.0, -0.5)    # left, top
    assert (pads["3"]["x_mm"], pads["3"]["y_mm"]) == (-3.0, 0.5)     # left, bottom
    assert (pads["4"]["x_mm"], pads["4"]["y_mm"]) == (-0.5, 3.0)     # bottom, left
    assert (pads["7"]["x_mm"], pads["7"]["y_mm"]) == (3.0, 0.5)      # right, bottom
    assert (pads["10"]["x_mm"], pads["10"]["y_mm"]) == (0.5, -3.0)   # top, right
    assert (pads["12"]["x_mm"], pads["12"]["y_mm"]) == (-0.5, -3.0)  # top, left


def test_qfn_pads_point_outward_on_each_side():
    pads = by_number(fp.qfn_pads(3, 0.5, 6.0, 0.8, 0.25))
    assert (pads["1"]["size_x_mm"], pads["1"]["size_y_mm"]) == (0.8, 0.25)   # left
    assert (pads["4"]["size_x_mm"], pads["4"]["size_y_mm"]) == (0.25, 0.8)   # bottom


def test_qfn_48_pin_with_thermal_pad_has_unique_numbers():
    pads = fp.qfn_pads(12, 0.5, 7.2, 0.85, 0.25, ep_width_mm=5.15, ep_height_mm=5.15)
    numbers = [p["number"] for p in pads]
    assert len(pads) == 49 and len(set(numbers)) == 49 and numbers[-1] == "49"


def test_qfn_rejects_bad_input():
    with pytest.raises(ValueError):
        fp.qfn_pads(0, 0.5, 6, 0.8, 0.25)
    with pytest.raises(ValueError):
        fp.qfn_pads(101, 0.5, 6, 0.8, 0.25)
    with pytest.raises(ValueError):
        fp.qfn_pads(3, 0.5, 6, 0, 0.25)


def test_rectangular_wqfn_matches_the_ti_rnp0030c_land_pattern():
    """30 pins: 11 per long side, 4 per end; the datasheet numbers the thermal pad 31."""
    pads = by_number(fp.qfn_pads(11, 0.5, 3.65, 0.75, 0.25, ep_width_mm=2.2, ep_height_mm=4.6,
                                 pins_top_bottom=4, span_y_mm=5.8, end_pad_length_mm=0.6))
    assert len(pads) == 31 and pads["31"]["size_y_mm"] == 4.6
    assert (pads["1"]["x_mm"], pads["1"]["y_mm"]) == (-1.825, -2.5)
    assert (pads["11"]["x_mm"], pads["11"]["y_mm"]) == (-1.825, 2.5)
    assert (pads["12"]["x_mm"], pads["12"]["y_mm"]) == (-0.75, 2.9)   # bottom, left
    assert (pads["12"]["size_x_mm"], pads["12"]["size_y_mm"]) == (0.25, 0.6)
    assert (pads["16"]["x_mm"], pads["16"]["y_mm"]) == (1.825, 2.5)   # right, bottom
    assert (pads["26"]["x_mm"], pads["26"]["y_mm"]) == (1.825, -2.5)
    assert (pads["27"]["x_mm"], pads["27"]["y_mm"]) == (0.75, -2.9)   # top, right
    assert (pads["30"]["x_mm"], pads["30"]["y_mm"]) == (-0.75, -2.9)


def test_square_qfn_defaults_are_unchanged_by_the_rectangular_options():
    square = fp.qfn_pads(3, 0.5, 6.0, 0.8, 0.25)
    explicit = fp.qfn_pads(3, 0.5, 6.0, 0.8, 0.25, pins_top_bottom=3, span_y_mm=6.0, end_pad_length_mm=0.8)
    assert square == explicit


# ===== tools + validation ===================================================== #


def test_dual_tool_writes_a_footprint_with_all_pads(mcp, blank_project):
    res = call(mcp, "create_footprint_dual", lib_name="ti", footprint_name="HTSSOP44",
               pin_count=44, pitch_mm=0.635, row_span_mm=7.0, pad_length_mm=1.5, pad_width_mm=0.35,
               ep_width_mm=4.31, ep_height_mm=8.26)
    assert res["pad_count"] == 45 and res["lib_id"] == "ti:HTSSOP44"
    mod = sch_io.parse_file(Path(res["kicad_mod_path"]))
    assert len(sch_io.find_children(mod, "pad")) == 45


def test_qfn_tool_footprint_is_searchable_without_reindex(mcp, blank_project):
    call(mcp, "create_footprint_qfn", lib_name="ti", footprint_name="VQFN48_RGZ",
         pins_per_side=12, pitch_mm=0.5, span_mm=7.2, pad_length_mm=0.85, pad_width_mm=0.25,
         ep_width_mm=5.15, ep_height_mm=5.15)
    from kicad_claude.tools import library as lib_tools
    assert "ti:VQFN48_RGZ" in lib_tools._ensure_index()["footprints"]


@needs_kicad
def test_kicad_accepts_generated_footprints(mcp, blank_project, tmp_path):
    res = call(mcp, "create_footprint_qfn", lib_name="ti", footprint_name="Q",
               pins_per_side=4, pitch_mm=0.5, span_mm=3.0, pad_length_mm=0.6, pad_width_mm=0.25)
    assert kicad_cli.validate_footprint(Path(res["kicad_mod_path"])) is True


@needs_kicad
def test_validation_rejects_a_broken_file_and_leaves_the_original_alone(tmp_path):
    bad = tmp_path / "bad.kicad_mod"
    bad.write_text("(footprint \"x\" (pad \"1\" smd rect (at 0 0)")
    before = bad.read_text()
    assert kicad_cli.validate_footprint(bad) is False
    assert bad.read_text() == before


@needs_kicad
def test_create_footprint_removes_a_file_kicad_rejects(tmp_path, monkeypatch):
    monkeypatch.setattr(kicad_cli, "validate_footprint", lambda path: False)
    with pytest.raises(ValueError, match="rejected"):
        lc.create_footprint(tmp_path, lib_name="x", footprint_name="F",
                            pads=[{"number": "1", "type": "smd", "shape": "rect", "x_mm": 0,
                                   "y_mm": 0, "size_x_mm": 1, "size_y_mm": 1}])
    assert not (tmp_path / "lib" / "x.pretty" / "F.kicad_mod").exists()
    assert not (tmp_path / "fp-lib-table").exists()  # nothing registered for a rejected file


@needs_kicad
def test_rectangular_qfn_tool_output_loads_in_kicad(mcp, blank_project):
    res = call(mcp, "create_footprint_qfn", lib_name="ti", footprint_name="WQFN30",
               pins_per_side=11, pitch_mm=0.5, span_mm=3.65, pad_length_mm=0.75, pad_width_mm=0.25,
               pins_top_bottom=4, span_y_mm=5.8, end_pad_length_mm=0.6,
               ep_width_mm=2.2, ep_height_mm=4.6)
    assert res["pad_count"] == 31
    assert kicad_cli.validate_footprint(Path(res["kicad_mod_path"])) is True
