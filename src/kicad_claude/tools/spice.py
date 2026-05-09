"""Phase 15 — SPICE simulation hooks.

Tools:
    export_spice_netlist      — KiCAD schematic → SPICE netlist
    run_ngspice_simulation    — run ngspice on a netlist + analysis directive

Requires `ngspice` on PATH for actual simulation. On macOS:
    brew install ngspice
On Linux:
    apt install ngspice  (or your distro equivalent)

The simulator output is captured verbatim — for plotting, parse the printed
data points yourself or pipe it into another tool.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from kicad_claude import state
from kicad_claude.adapters import kicad_cli

logger = logging.getLogger("kicad-claude.tools.spice")


def _find_ngspice() -> Path | None:
    found = shutil.which("ngspice")
    return Path(found) if found else None


def register(mcp) -> None:
    @mcp.tool()
    def export_spice_netlist(
        output_path: str | None = None,
        timeout_seconds: float = 60.0,
    ) -> dict:
        """Export the active project's schematic as a SPICE netlist.

        Default output: `<project>/fab/<project>-spice.cir`. Run
        `run_ngspice_simulation` on the result to perform DC/AC/transient
        analysis.

        Note: for SPICE to be meaningful, schematic symbols must have
        `Spice_Model` / `Spice_Lib_File` / `Spice_Primitive` fields set.
        Symbols from KiCAD's official Simulation library (`Simulation_SPICE`)
        already include these.
        """
        proj = state.get_active()
        out = (
            Path(output_path).expanduser()
            if output_path
            else proj.path / "fab" / f"{proj.name}-spice.cir"
        )
        out.parent.mkdir(parents=True, exist_ok=True)
        kicad_cli.export_netlist(
            proj.sch_path, out, fmt="spice", timeout=timeout_seconds,
        )
        return {
            "netlist_path": str(out),
            "size_bytes": out.stat().st_size,
            "format": "spice",
        }

    @mcp.tool()
    def run_ngspice_simulation(
        netlist_path: str | None = None,
        analysis: str = ".op",
        output_path: str | None = None,
        timeout_seconds: float = 120.0,
    ) -> dict:
        """Run ngspice in batch mode on a netlist file.

        `analysis` is a SPICE control directive, for example:
            - `.op`                            — DC operating point
            - `.dc V1 0 5 0.1`                 — DC sweep V1 from 0 to 5 V
            - `.ac dec 10 1 1G`                — AC sweep, log scale
            - `.tran 1u 10m`                   — transient, step 1 µs over 10 ms
            - `.noise V(out) V1 dec 10 1 1G`   — noise analysis

        Returns the simulator's stdout/stderr verbatim so the caller can
        parse data points or error messages.
        """
        ngspice = _find_ngspice()
        if ngspice is None:
            raise FileNotFoundError(
                "ngspice not found on PATH. Install it (macOS: `brew install ngspice`; "
                "Linux: package manager) or set NGSPICE env var."
            )

        proj = state.get_active()
        if netlist_path is None:
            netlist_path = str(proj.path / "fab" / f"{proj.name}-spice.cir")
        nl_path = Path(netlist_path).expanduser().resolve()
        if not nl_path.is_file():
            raise FileNotFoundError(
                f"netlist not found at {nl_path}. Run export_spice_netlist first."
            )

        out = (
            Path(output_path).expanduser()
            if output_path
            else proj.path / "fab" / f"{proj.name}-spice-out.txt"
        )
        out.parent.mkdir(parents=True, exist_ok=True)

        # ngspice batch mode: -b runs the netlist non-interactively. Append the
        # analysis directive + .end to a temp file so we don't mutate the user's.
        composed = (
            f"* kicad-claude SPICE driver\n"
            + nl_path.read_text(encoding="utf-8", errors="replace")
            + f"\n{analysis}\n.print all\n.end\n"
        )
        driver_path = out.with_suffix(".cir")
        driver_path.write_text(composed)

        try:
            r = subprocess.run(
                [str(ngspice), "-b", str(driver_path), "-o", str(out)],
                capture_output=True, text=True,
                timeout=timeout_seconds, check=False,
            )
        except subprocess.TimeoutExpired as e:
            raise TimeoutError(
                f"ngspice timed out after {timeout_seconds}s"
            ) from e

        return {
            "returncode": r.returncode,
            "ngspice_path": str(ngspice),
            "driver_path": str(driver_path),
            "output_path": str(out) if out.is_file() else None,
            "analysis": analysis,
            "stdout_tail": r.stdout[-2000:] if r.stdout else "",
            "stderr_tail": r.stderr[-2000:] if r.stderr else "",
        }
