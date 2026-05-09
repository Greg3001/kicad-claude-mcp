# KiCAD-Claude MCP

MCP server that lets Claude Code design KiCAD PCBs by editing project files
directly. You talk to Claude Code in your terminal; the `.kicad_sch` and
`.kicad_pcb` files are modified in place. You open KiCAD to verify.

Full implementation spec: [`kicad-claude-mcp-spec.md`](./kicad-claude-mcp-spec.md).

## Status

**All phases complete (0–8).** 42 MCP tools registered. 93 fast tests +
14 acceptance tests pass.

| Phase | What it adds | Tools |
|------|--------------|-------|
| 0 | Bootstrap | `ping` |
| 1 | Project management | `create_project`, `set_project`, `get_project_state`, `list_components` |
| 2 | KiCAD library indexer + fuzzy search | `index_libraries`, `list_libraries`, `search_symbol`, `search_footprint`, `get_symbol_details` |
| 3 | Schematic editing | `add_symbol`, `remove_symbol`, `move_symbol`, `add_wire`, `add_label`, `add_power_symbol`, `add_no_connect`, `list_pins`, `get_pin_position` |
| 4 | External sourcing (DigiKey, Mouser, vendor ZIP import) | `check_availability`, `find_or_fetch_symbol`, `import_vendor_zip`, `list_vendor_parts` |
| 5 | PCB editing | `set_layer_count`, `set_board_outline`, `list_footprints`, `add_footprint`, `move_footprint`, `place_footprints_grid`, `add_track`, `add_via` |
| 6 | Autorouting via Freerouting 2.1.0 | `autoroute_pcb`, `export_dsn`, `import_ses` |
| 7 | Validation (ERC/DRC via `kicad-cli`) | `run_erc`, `run_drc` |
| 8 | Hierarchical sheets + 2-32 layer PCBs (extra-spec) | `add_sheet`, `set_active_sheet`, `get_active_sheet`, `list_sheets`, `add_hierarchical_label`, `add_sheet_pin` |

Phase-by-phase notes and the technical decisions that diverge from the spec
live in [`docs/PROGRESS.md`](./docs/PROGRESS.md).

## Requirements

- Python 3.10+
- [`uv`](https://docs.astral.sh/uv/) (fast package manager)
- KiCAD 9.0+ with `kicad-cli` available (Phase 5+)
- Java 21+ (Phase 6, Freerouting)

## Setup

```bash
uv sync
cp .env.example .env       # fill in API keys when needed (Phase 4+)
```

## Develop

Open the MCP Inspector to test tools in isolation:

```bash
uv run mcp dev server.py
```

## Use from Claude Code

Add to `~/.claude/settings.json` (or `.claude/settings.json` inside your KiCAD
project directory):

```json
{
  "mcpServers": {
    "kicad": {
      "command": "/ABSOLUTE/PATH/TO/KiCAD_PlugIN/.venv/bin/python",
      "args": ["server.py"],
      "cwd": "/ABSOLUTE/PATH/TO/KiCAD_PlugIN"
    }
  }
}
```

Then reload Claude Code, run `/mcp` to confirm `kicad` is connected, and:

```
> Use the kicad ping tool
< pong
```

**Critical**: the `command` must be the absolute path to `.venv/bin/python` of
this project. Using the system Python or a relative path fails to load the
dependencies.

## Project layout

See §4 of the spec. Top-level:

```
KiCAD_PlugIN/
├── server.py                  # MCP entry point
├── pyproject.toml
├── .env.example
├── src/kicad_claude/
│   ├── tools/                 # MCP tools, grouped by phase
│   ├── adapters/              # wrappers around external tools (kicad-cli, freerouting, …)
│   ├── indexer/               # KiCAD library indexing
│   └── utils/                 # logging, geometry, paths
├── tests/
├── docs/
├── vendor_parts/              # ZIPs from SnapEDA / Ultra Librarian
└── third_party/               # freerouting.jar, gitignored
```

## Caveats

- Logging **must** go to stderr (stdout is used by the MCP STDIO transport).
  Use `from src.kicad_claude.utils.logging import setup_logging`. Never
  `print()` to stdout from server-side code.
- KiCAD must be **closed** while tools modify the project files — KiCAD locks
  them while open and may overwrite changes.
