"""Phase 6 — autorouting tools.

Tools:
    autoroute_pcb     — full pipeline: export DSN → run Freerouting → import SES
    export_dsn        — export-only (use the GUI of Freerouting if you want)
    import_ses        — import an existing .ses back into the active PCB

The DSN export and SES import shell out to KiCAD's bundled Python 3.9 because
`kicad-cli` v10 doesn't expose Specctra interchange. Freerouting itself is
invoked as a JAR via `java`.
"""

from __future__ import annotations

import logging
from pathlib import Path

from kicad_claude import state
from kicad_claude.adapters import freerouting, kicad_python, sch_editor

logger = logging.getLogger("kicad-claude.tools.routing")


def register(mcp) -> None:
    """Register Phase 6 tools on the FastMCP instance."""

    @mcp.tool()
    def export_dsn(output_path: str | None = None) -> dict:
        """Export the active PCB to Specctra DSN.

        If `output_path` is None, writes alongside the .kicad_pcb with the
        same stem and `.dsn` extension.
        """
        proj = state.get_active()
        out = (
            Path(output_path).expanduser()
            if output_path
            else proj.pcb_path.with_suffix(".dsn")
        )
        kicad_python.export_dsn(proj.pcb_path, out)
        return {"dsn_path": str(out), "size_bytes": out.stat().st_size}

    @mcp.tool()
    def import_ses(ses_path: str) -> dict:
        """Apply a Freerouting `.ses` session into the active `.kicad_pcb` (in-place).

        A backup of the PCB is saved under `<project>/.backups/` before the import.
        """
        proj = state.get_active()
        ses = Path(ses_path).expanduser()
        backup = sch_editor.backup_file(proj.pcb_path)
        kicad_python.import_ses(proj.pcb_path, ses)
        return {
            "pcb_path": str(proj.pcb_path),
            "ses_path": str(ses),
            "backup": str(backup) if backup else None,
        }

    @mcp.tool()
    def autoroute_pcb(
        passes: int = 100,
        timeout_seconds: float = 300.0,
        threads: int | None = None,
    ) -> dict:
        """Full autoroute pipeline: export DSN → Freerouting → import SES.

        Args:
            passes: Freerouting `-mp` value. Higher = better routes, slower.
            timeout_seconds: Hard kill if Freerouting runs longer than this.
            threads: optional `-mt` threads count.
        """
        proj = state.get_active()
        dsn_path = proj.pcb_path.with_suffix(".dsn")
        ses_path = proj.pcb_path.with_suffix(".ses")

        # 1) Export DSN
        kicad_python.export_dsn(proj.pcb_path, dsn_path)

        # 2) Run Freerouting
        route_result = freerouting.route(
            dsn_path,
            ses_path,
            passes=passes,
            threads=threads,
            timeout_seconds=timeout_seconds,
        )

        # 3) Backup and import SES back into the PCB
        backup = sch_editor.backup_file(proj.pcb_path)
        kicad_python.import_ses(proj.pcb_path, ses_path)

        return {
            "pcb_path": str(proj.pcb_path),
            "dsn_path": str(dsn_path),
            "ses_path": str(ses_path),
            "passes": passes,
            "stats": route_result["stats"],
            "freerouting_returncode": route_result["returncode"],
            "freerouting_stdout_tail": route_result["stdout_tail"],
            "backup": str(backup) if backup else None,
        }
