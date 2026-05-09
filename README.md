# KiCAD-Claude MCP

MCP server that lets Claude Code design KiCAD PCBs by editing project files
directly. You talk to Claude Code in your terminal; the `.kicad_sch` and
`.kicad_pcb` files are modified in place. You open KiCAD to verify.

Full implementation spec: [`kicad-claude-mcp-spec.md`](./kicad-claude-mcp-spec.md).

## Status

**Phase 0 — Bootstrap.** Only the `ping` tool is wired up. Real KiCAD tools
land in subsequent phases (project management, library indexing, schematic
editing, sourcing, PCB layout, autorouting, validation).

Phase progress is tracked in [`docs/PROGRESS.md`](./docs/PROGRESS.md).

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
