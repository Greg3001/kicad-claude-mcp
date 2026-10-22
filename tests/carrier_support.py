"""Shared helpers for the phase-16 (SOM-carrier) tests: real KiCad round trips."""

from __future__ import annotations

import json
import re
import subprocess
from collections import Counter
from pathlib import Path

import pytest

from kicad_claude.adapters import sch_editor as ed
from kicad_claude.adapters import sch_io
from kicad_claude.utils.kicad_paths import find_kicad_cli

FIXTURES = Path(__file__).parent / "fixtures"
KICAD_CLI = find_kicad_cli()
needs_kicad = pytest.mark.skipif(KICAD_CLI is None, reason="kicad-cli not installed")


def call(server, _tool: str, **kwargs):
    """Invoke a registered MCP tool function directly (`name=` stays free for tools)."""
    return server._tool_manager.get_tool(_tool).fn(**kwargs)


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(KICAD_CLI), *args], capture_output=True, text=True, timeout=180, check=False
    )


def erc_report(sch_path: Path) -> list[dict]:
    """All ERC violations of a project (every sheet), as kicad-cli reports them."""
    out = sch_path.with_suffix(".erc.json")
    _run_cli("sch", "erc", "--format", "json", "--severity-all", "-o", str(out), str(sch_path))
    report = json.loads(out.read_text())
    return [v for sh in report.get("sheets", []) for v in sh.get("violations", [])]


def erc_counts(sch_path: Path) -> Counter:
    return Counter(v["type"] for v in erc_report(sch_path))


def netlist_pins(sch_path: Path) -> dict[str, set[tuple[str, str]]]:
    """Export a kicadxml netlist and return {net name: {(ref, pin)}}."""
    out = sch_path.with_suffix(".xml")
    _run_cli("sch", "export", "netlist", "--format", "kicadxml", "-o", str(out), str(sch_path))
    nets = {}
    for name, body in re.findall(
        r'<net code="\d+" name="([^"]+)"[^>]*>(.*?)</net>', out.read_text(), re.S
    ):
        nets[name] = set(re.findall(r'<node ref="([^"]+)" pin="([^"]+)"', body))
    return nets


def refs_on(path: Path) -> list[str]:
    return ed.all_references(sch_io.parse_file(path))
