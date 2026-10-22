"""Fixtures for the phase-16 tests (a fresh project and a tool server)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from mcp.server.fastmcp import FastMCP

from kicad_claude import state
from kicad_claude.indexer import kicad_libs
from kicad_claude.templates.blank import write_blank_project
from kicad_claude.tools import library as lib_tools
from kicad_claude.tools import library_create as create_tools
from kicad_claude.tools import schematic as sch_tools

FIXTURE_LIBS = Path(__file__).parent / "fixtures"


@pytest.fixture
def blank_project(tmp_path: Path):
    state.clear_active()
    files = write_blank_project(tmp_path / "demo", "demo")
    state.set_active(tmp_path / "demo", "demo")
    yield files
    state.clear_active()


@pytest.fixture
def mcp(blank_project, tmp_path, monkeypatch):
    """FastMCP with schematic + library tools over a fixture index.

    The fixture index holds MiniLib, UnitLib and a tiny `power` library; the
    project's own `lib/` is overlaid by the real `_ensure_index`.
    """
    syms = tmp_path / "syms"
    syms.mkdir()
    for lib in FIXTURE_LIBS.glob("*.kicad_sym"):
        shutil.copy(lib, syms / lib.name)
    idx = kicad_libs.build_index(symbol_dirs=[syms], footprint_dirs=[])
    monkeypatch.setattr(lib_tools, "load_cache", lambda: idx)
    monkeypatch.setattr(lib_tools, "_index", None)
    server = FastMCP("test")
    sch_tools.register(server)
    lib_tools.register(server)
    create_tools.register(server)
    return server
