"""Tool: place the PWR_FLAGs ERC asks for.

    add_power_flags  - one PWR_FLAG per undriven power net, found via ERC
"""

from __future__ import annotations

import json
import logging

from kicad_claude import state
from kicad_claude.adapters import kicad_cli, power_flags
from kicad_claude.adapters import sch_editor as ed
from kicad_claude.adapters import sch_io
from kicad_claude.tools import schematic as sch_tools

logger = logging.getLogger("kicad-claude.tools.power_flags")


def _erc_raw(timeout: float) -> dict:
    proj = state.get_active()
    result = kicad_cli.run_erc(proj.sch_path, timeout=timeout)
    return json.loads(open(result["raw_path"], encoding="utf-8").read())


def _locate(sheet_path: str, ref: str) -> tuple[str | None, str, tuple[float, float]]:
    """(sheet filename or None for root, net name, pin position) of a power symbol."""
    proj = state.get_active()
    name = power_flags.sheet_name_of(sheet_path)
    root = sch_io.parse_file(proj.sch_path)
    filename = None
    path = proj.sch_path
    if name is not None:
        node = ed.find_sheet_by_name(root, name)
        if node is None:
            raise KeyError(f"ERC names sheet {name!r}, which the root does not have")
        filename = ed.get_sheet_filename(node)
        path = proj.path / (filename or "")
    tree = sch_io.parse_file(path)
    symbol = ed.find_symbol_by_reference(tree, ref)
    if symbol is None:
        raise KeyError(f"ERC names {ref!r}, which is not on sheet {sheet_path!r}")
    net = ed.get_symbol_property(symbol, "Value") or ""
    x, y = ed.get_pin_position(tree, ref, "1")
    return filename, net, (x, y)


def register(mcp) -> None:
    @mcp.tool()
    def add_power_flags(timeout_seconds: float = 120.0) -> dict:
        """Place one PWR_FLAG on every power net ERC reports as undriven.

        Runs ERC, finds each `power_pin_not_driven`, and drops a PWR_FLAG on
        the first power symbol of each such net (one per net satisfies every
        symbol on it, on all sheets). The flag sits on the symbol's pin, so it
        overlaps the drawing; move it if you want the sheet tidy. Re-runs ERC
        and reports how many undriven pins remain.

        A flag only declares "this net is supplied from outside"; do not add
        one to a rail that a regulator on the board is meant to drive.
        """
        targets = power_flags.undriven_refs(_erc_raw(timeout_seconds))
        found = []
        for sheet_path, ref in targets:
            filename, net, pos = _locate(sheet_path, ref)
            found.append({"sheet": filename, "ref": ref, "net": net, "pos": pos})
        chosen = {}
        for item in found:  # one flag per net is enough
            chosen.setdefault(item["net"], item)
        placed = []
        previous = state.get_active_sheet_filename()
        try:
            for net, item in chosen.items():
                state.set_active_sheet(item["sheet"])
                x, y = item["pos"]
                flag = sch_tools.place_power_symbol("PWR_FLAG", x, y)
                placed.append({"net": net, "sheet": item["sheet"] or "root",
                               "position_mm": [x, y], "reference": flag["reference"]})
        finally:
            state.set_active_sheet(previous)
        remaining = len(power_flags.undriven_refs(_erc_raw(timeout_seconds))) if placed else len(targets)
        return {"flags_placed": len(placed), "flags": placed,
                "undriven_before": len(targets), "undriven_after": remaining}
