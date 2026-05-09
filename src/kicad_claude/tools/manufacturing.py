"""Phase 9 — manufacturing outputs and visualization.

Tools (8):
    export_gerbers        — Gerber files for fab houses (per-layer .gbr)
    export_drill          — Excellon drill files (PTH + NPTH separately by default)
    export_pos            — Pick-and-place position file (CSV by default)
    export_bom            — Bill of Materials (CSV from the schematic)
    export_netlist        — Netlist (kicadsexpr / spice / orcadpcb2 / …)
    render_pcb_3d         — PNG/JPEG 3D render of the PCB
    export_pcb_svg        — One SVG per copper/silk/etc. layer
    export_fab_package    — Bundled gerbers + drill + pos + bom + render in <project>/fab/

All tools default to writing into `<project>/fab/` so a single project ends
up with a clean, fab-house-ready directory.
"""

from __future__ import annotations

import logging
from pathlib import Path

from kicad_claude import state
from kicad_claude.adapters import kicad_cli

logger = logging.getLogger("kicad-claude.tools.manufacturing")


def _fab_dir(subdir: str | None = None) -> Path:
    """Default output directory for manufacturing artifacts."""
    proj = state.get_active()
    base = proj.path / "fab"
    if subdir:
        base = base / subdir
    base.mkdir(parents=True, exist_ok=True)
    return base


def register(mcp) -> None:
    """Register Phase 9 tools on the FastMCP instance."""

    # ----- Individual exports ----------------------------------------------- #

    @mcp.tool()
    def export_gerbers(
        output_dir: str | None = None,
        layers: str | None = None,
        timeout_seconds: float = 120.0,
    ) -> dict:
        """Export Gerber files for fabrication.

        `layers` is an optional comma-separated subset (e.g. "F.Cu,B.Cu,Edge.Cuts").
        When omitted, KiCAD uses the project's plot settings.
        Defaults to `<project>/fab/gerbers/`.
        """
        out = Path(output_dir).expanduser() if output_dir else _fab_dir("gerbers")
        layer_list = [s.strip() for s in layers.split(",")] if layers else None
        return kicad_cli.export_gerbers(
            state.get_active_board_path(), out, layers=layer_list, timeout=timeout_seconds
        )

    @mcp.tool()
    def export_drill(
        output_dir: str | None = None,
        format: str = "excellon",
        generate_map: bool = True,
        map_format: str = "pdf",
        separate_pth_npth: bool = True,
        timeout_seconds: float = 60.0,
    ) -> dict:
        """Export drill files (Excellon by default) + drill map.

        `separate_pth_npth=True` writes PTH and NPTH holes to separate files,
        which is what most fab houses prefer.
        """
        proj = state.get_active()
        out = Path(output_dir).expanduser() if output_dir else _fab_dir("drill")
        return kicad_cli.export_drill(
            state.get_active_board_path(), out,
            drill_format=format,
            generate_map=generate_map,
            map_format=map_format,
            excellon_separate_th=separate_pth_npth,
            timeout=timeout_seconds,
        )

    @mcp.tool()
    def export_pos(
        output_path: str | None = None,
        side: str = "both",
        format: str = "csv",
        units: str = "mm",
        smd_only: bool = False,
        exclude_dnp: bool = False,
        timeout_seconds: float = 60.0,
    ) -> dict:
        """Export pick-and-place position file (for PCB assembly).

        `side`: front | back | both
        `format`: csv | ascii | gerber
        Defaults to `<project>/fab/<project>-pos.csv`.
        """
        proj = state.get_active()
        if output_path:
            out = Path(output_path).expanduser()
        else:
            ext = "csv" if format == "csv" else ("pos" if format == "ascii" else "gbr")
            out = _fab_dir() / f"{proj.name}-pos.{ext}"
        return kicad_cli.export_pos(
            state.get_active_board_path(), out,
            side=side, fmt=format, units=units,
            smd_only=smd_only, exclude_dnp=exclude_dnp,
            timeout=timeout_seconds,
        )

    @mcp.tool()
    def export_bom(
        output_path: str | None = None,
        fields: str | None = None,
        group_by: str | None = "Value",
        exclude_dnp: bool = False,
        timeout_seconds: float = 60.0,
    ) -> dict:
        """Export the Bill of Materials from the schematic as CSV.

        `fields`: comma-separated, e.g. "Reference,Value,Footprint,QUANTITY,DNP".
        Default is the standard KiCAD set.
        """
        proj = state.get_active()
        out = (
            Path(output_path).expanduser()
            if output_path
            else _fab_dir() / f"{proj.name}-bom.csv"
        )
        return kicad_cli.export_bom(
            proj.sch_path, out,
            fields=fields, group_by=group_by,
            exclude_dnp=exclude_dnp,
            timeout=timeout_seconds,
        )

    @mcp.tool()
    def export_netlist(
        output_path: str | None = None,
        format: str = "kicadsexpr",
        timeout_seconds: float = 60.0,
    ) -> dict:
        """Export a netlist from the schematic.

        `format`: kicadsexpr | kicadxml | cadstar | orcadpcb2 | spice | spicemodel | pads | allegro
        """
        proj = state.get_active()
        ext = {
            "kicadsexpr": "net", "kicadxml": "xml",
            "spice": "cir", "spicemodel": "lib",
            "orcadpcb2": "net", "cadstar": "frp",
            "pads": "asc", "allegro": "txt",
        }.get(format, "txt")
        out = (
            Path(output_path).expanduser()
            if output_path
            else _fab_dir() / f"{proj.name}-netlist.{ext}"
        )
        return kicad_cli.export_netlist(
            proj.sch_path, out, fmt=format, timeout=timeout_seconds
        )

    @mcp.tool()
    def render_pcb_3d(
        output_path: str | None = None,
        side: str = "top",
        width: int = 1600,
        height: int = 900,
        quality: str = "basic",
        rotate: str | None = None,
        perspective: bool = False,
        timeout_seconds: float = 180.0,
    ) -> dict:
        """Render the PCB in 3D as a PNG (or JPEG if path ends with .jpg).

        `side`: top | bottom | left | right | front | back
        `quality`: basic | high   (high is much slower but prettier)
        `rotate`: 'X,Y,Z' degrees, e.g. '-30,0,45' for an isometric look.
        """
        proj = state.get_active()
        out = (
            Path(output_path).expanduser()
            if output_path
            else _fab_dir() / f"{proj.name}-render-{side}.png"
        )
        return kicad_cli.render_pcb(
            state.get_active_board_path(), out,
            side=side, width=width, height=height,
            quality=quality, rotate=rotate, perspective=perspective,
            timeout=timeout_seconds,
        )

    @mcp.tool()
    def export_step_3d(
        output_path: str | None = None,
        include_components: bool = True,
        include_tracks: bool = False,
        include_zones: bool = False,
        include_silkscreen: bool = False,
        include_soldermask: bool = False,
        no_dnp: bool = False,
        component_filter: str | None = None,
        timeout_seconds: float = 240.0,
    ) -> dict:
        """Export the PCB as a STEP 3D file (for Fusion 360 / SolidWorks / FreeCAD).

        Default: board body + 3D component models. Toggle include_tracks,
        include_zones, include_silkscreen, include_soldermask to add copper
        and graphical layers to the STEP.

        `component_filter` accepts wildcards (e.g. "U*,R1,R2").
        Big boards with everything included can take several minutes.
        """
        proj = state.get_active()
        out = (
            Path(output_path).expanduser()
            if output_path
            else _fab_dir() / f"{proj.name}.step"
        )
        return kicad_cli.export_step(
            state.get_active_board_path(), out,
            include_components=include_components,
            include_tracks=include_tracks,
            include_zones=include_zones,
            include_silkscreen=include_silkscreen,
            include_soldermask=include_soldermask,
            no_dnp=no_dnp,
            component_filter=component_filter,
            timeout=timeout_seconds,
        )

    @mcp.tool()
    def export_pcb_svg(
        output_dir: str | None = None,
        layers: str | None = None,
        fit_page_to_board: bool = True,
        black_and_white: bool = False,
        timeout_seconds: float = 60.0,
    ) -> dict:
        """Export one SVG per layer for documentation/preview.

        `layers`: comma-separated subset (e.g. "F.Cu,B.Cu,F.SilkS"). When
        omitted, all visible layers are written.
        """
        proj = state.get_active()
        out = Path(output_dir).expanduser() if output_dir else _fab_dir("svg")
        layer_list = [s.strip() for s in layers.split(",")] if layers else None
        return kicad_cli.export_pcb_svg(
            state.get_active_board_path(), out,
            layers=layer_list,
            fit_page_to_board=fit_page_to_board,
            black_and_white=black_and_white,
            timeout=timeout_seconds,
        )

    # ----- Bundled fab package --------------------------------------------- #

    @mcp.tool()
    def export_fab_package(
        output_dir: str | None = None,
        include_render: bool = True,
        include_svg: bool = False,
        timeout_seconds: float = 240.0,
    ) -> dict:
        """One-shot: export gerbers + drill + pos + BOM + (optional) render to `<project>/fab/`.

        Produces a directory ready to zip and send to a fab house. If
        `include_render` is True, also writes a 3D render of the top side.
        """
        proj = state.get_active()
        base = Path(output_dir).expanduser() if output_dir else _fab_dir()
        base.mkdir(parents=True, exist_ok=True)

        results: dict = {"output_dir": str(base), "steps": {}}

        # Gerbers
        results["steps"]["gerbers"] = kicad_cli.export_gerbers(
            state.get_active_board_path(), base / "gerbers", timeout=timeout_seconds,
        )
        # Drill
        results["steps"]["drill"] = kicad_cli.export_drill(
            state.get_active_board_path(), base / "drill",
            generate_map=True, excellon_separate_th=True,
            timeout=timeout_seconds,
        )
        # Position file
        try:
            results["steps"]["pos"] = kicad_cli.export_pos(
                state.get_active_board_path(), base / f"{proj.name}-pos.csv",
                side="both", fmt="csv", units="mm",
                timeout=timeout_seconds,
            )
        except kicad_cli.KicadCliError as e:
            results["steps"]["pos"] = {"error": str(e)}
        # BOM
        try:
            results["steps"]["bom"] = kicad_cli.export_bom(
                proj.sch_path, base / f"{proj.name}-bom.csv",
                timeout=timeout_seconds,
            )
        except kicad_cli.KicadCliError as e:
            results["steps"]["bom"] = {"error": str(e)}
        # Render (optional)
        if include_render:
            try:
                results["steps"]["render"] = kicad_cli.render_pcb(
                    state.get_active_board_path(), base / f"{proj.name}-top.png",
                    side="top", timeout=timeout_seconds,
                )
            except kicad_cli.KicadCliError as e:
                results["steps"]["render"] = {"error": str(e)}
        if include_svg:
            try:
                results["steps"]["svg"] = kicad_cli.export_pcb_svg(
                    state.get_active_board_path(), base / "svg",
                    fit_page_to_board=True,
                    timeout=timeout_seconds,
                )
            except kicad_cli.KicadCliError as e:
                results["steps"]["svg"] = {"error": str(e)}

        # Summary counts
        all_files: list[str] = []
        for step in results["steps"].values():
            if not isinstance(step, dict) or "error" in step:
                continue
            if "files" in step:
                all_files.extend(step["files"])
            elif "output_path" in step:
                all_files.append(Path(step["output_path"]).name)
        results["total_artifact_count"] = len(all_files)
        return results
