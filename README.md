# KiCAD-Claude MCP

MCP server that lets Claude Code design KiCAD PCBs by editing project files
directly. You talk to Claude Code in your terminal; the `.kicad_sch` and
`.kicad_pcb` files are modified in place. You open KiCAD to verify.

Full implementation spec: [`kicad-claude-mcp-spec.md`](./kicad-claude-mcp-spec.md).

## Status

**Phases 0–14 complete.** 99 MCP tools registered. 190 fast tests +
30 acceptance tests pass.

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
| 9 | Manufacturing outputs (gerbers, drill, BOM, 3D render, SVG) | `export_gerbers`, `export_drill`, `export_pos`, `export_bom`, `export_netlist`, `render_pcb_3d`, `export_pcb_svg`, `export_fab_package` |
| 10 | Design rules + net classes + annotation + schematic↔PCB sync | `set_design_rules`, `apply_fab_preset`, `list_fab_presets`, `add_net_class`, `remove_net_class`, `assign_net_class`, `list_net_classes`, `annotate_schematic`, `update_pcb_from_schematic` |
| 11 | Copper zones, mounting holes, silk, fiducials, sourcing-enriched BOM | `add_zone`, `add_ground_plane`, `add_silk_text`, `add_mounting_hole`, `add_fiducial`, `enrich_bom_with_sourcing` |
| 12 | Diff pairs, length tuning (meander), schematic buses | `list_diff_pair_candidates`, `add_diff_pair_class`, `list_nets`, `compute_trace_length`, `validate_diff_pair_length_match`, `add_meander`, `add_bus`, `add_bus_entry`, `add_bus_alias` |
| 13 | STEP 3D, custom DRC rules, multi-board, symbol/footprint creation | `export_step_3d`, `add_drc_rule`, `list_drc_rules`, `remove_drc_rule`, `clear_drc_rules`, `add_board`, `list_boards`, `set_active_board`, `create_symbol`, `create_footprint` |
| 14 | Signal integrity (impedance), thermal (IPC-2152), RF, EMC heuristics | `calculate_microstrip_impedance`, `calculate_stripline_impedance`, `calculate_differential_impedance`, `calculate_coplanar_waveguide_impedance`, `solve_trace_width_for_impedance`, `list_impedance_targets`, `calculate_trace_current_capacity`, `solve_trace_width_for_current`, `analyze_pcb_current_capacity`, `add_via_array`, `add_ground_stitching`, `add_rf_microstrip`, `analyze_ground_coverage`, `find_long_traces`, `validate_decoupling_caps` |

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
