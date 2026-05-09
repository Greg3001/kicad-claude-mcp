"""Phase 7 — validation tools (ERC / DRC).

Tools:
    run_erc   — Electrical Rules Check on the active schematic
    run_drc   — Design Rules Check on the active PCB

Both shell out to `kicad-cli` and return structured JSON (errors,
warnings, violations with positions). Raw report JSON is also written next
to the source file so it can be inspected by humans.
"""

from __future__ import annotations

import logging

from kicad_claude import state
from kicad_claude.adapters import kicad_cli

logger = logging.getLogger("kicad-claude.tools.validation")


def register(mcp) -> None:
    """Register Phase 7 tools on the FastMCP instance."""

    @mcp.tool()
    def run_erc(severity: str = "all", timeout_seconds: float = 60.0) -> dict:
        """Run KiCAD's Electrical Rules Check on the active schematic.

        Args:
            severity: 'all', 'error', 'warning', or 'exclusions'. Maps to
                `kicad-cli sch erc --severity-<value>`.
            timeout_seconds: hard cap on the kicad-cli invocation.

        Returns counts by severity, the list of violations with positions,
        and the path to the raw JSON report.
        """
        proj = state.get_active()
        return kicad_cli.run_erc(
            proj.sch_path, severity=severity, timeout=timeout_seconds
        )

    @mcp.tool()
    def run_drc(
        severity: str = "all",
        schematic_parity: bool = True,
        all_track_errors: bool = False,
        timeout_seconds: float = 120.0,
    ) -> dict:
        """Run KiCAD's Design Rules Check on the active PCB.

        Args:
            severity: 'all', 'error', 'warning', or 'exclusions'.
            schematic_parity: include parity check between PCB and schematic.
            all_track_errors: report each individual track error (more verbose).
            timeout_seconds: hard cap.

        Returns counts, violations, unconnected items, parity findings, and
        the path to the raw JSON report.
        """
        proj = state.get_active()
        return kicad_cli.run_drc(
            proj.pcb_path,
            severity=severity,
            schematic_parity=schematic_parity,
            all_track_errors=all_track_errors,
            timeout=timeout_seconds,
        )
