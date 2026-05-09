"""Wrapper for `kicad-cli` invocations the project relies on.

Currently exposes ERC (`sch erc`) and DRC (`pcb drc`) with structured JSON
parsing. Both subcommands are present in KiCAD 9.0+ and 10.x.
"""

from __future__ import annotations

import json
import logging
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from kicad_claude.utils.kicad_paths import find_kicad_cli

logger = logging.getLogger("kicad-claude.adapters.kicad_cli")


class KicadCliError(RuntimeError):
    pass


def _ensure_cli() -> Path:
    cli = find_kicad_cli()
    if cli is None:
        raise KicadCliError(
            "`kicad-cli` not found on PATH and not in the standard install path. "
            "Add it to PATH (macOS: /Applications/KiCad/KiCad.app/Contents/MacOS) "
            "or set KICAD_CLI in the environment."
        )
    return cli


def _run(args: list[str], *, timeout: float = 120.0, cwd: Path | None = None) -> tuple[str, str]:
    """Invoke `kicad-cli` with `args`. Returns (stdout, stderr) on success."""
    cli = _ensure_cli()
    cmd = [str(cli), *args]
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout, check=False, cwd=str(cwd) if cwd else None,
        )
    except subprocess.TimeoutExpired as e:
        raise KicadCliError(f"kicad-cli timed out after {timeout}s: {' '.join(cmd[1:4])}…") from e
    if r.returncode != 0:
        raise KicadCliError(
            f"kicad-cli {' '.join(args[:3])} failed (rc={r.returncode}). "
            f"stderr: {r.stderr[-300:]}"
        )
    return r.stdout, r.stderr


def _list_files(p: Path) -> list[str]:
    """Names of regular files directly inside `p`. Returns [] if p doesn't exist."""
    if not p.is_dir():
        return []
    return sorted(f.name for f in p.iterdir() if f.is_file())


def _summarize_violations(violations: list[dict]) -> Counter:
    """Count violations by severity ('error', 'warning', 'exclusion', ...)."""
    counts: Counter = Counter()
    for v in violations:
        sev = (v.get("severity") or "unknown").lower()
        counts[sev] += 1
    return counts


def _shape_violation(v: dict) -> dict:
    """Trim a raw kicad-cli violation to the fields useful to the caller."""
    items = v.get("items") or []
    return {
        "type": v.get("type") or v.get("error_type") or "",
        "severity": (v.get("severity") or "").lower(),
        "description": v.get("description", ""),
        "items": [
            {
                "uuid": it.get("uuid", ""),
                "description": it.get("description", ""),
                "position": [
                    (it.get("pos") or {}).get("x"),
                    (it.get("pos") or {}).get("y"),
                ] if it.get("pos") else None,
            }
            for it in items
        ],
    }


# --------------------------------------------------------------------------- #
# ERC
# --------------------------------------------------------------------------- #


def run_erc(
    sch_path: Path,
    *,
    output_json: Path | None = None,
    severity: str = "all",
    timeout: float = 60.0,
) -> dict:
    """Run `kicad-cli sch erc --format json` and parse the report.

    Returns a dict with: counts (by severity), violations (shaped), raw_path
    (where the full JSON lives), and metadata (kicad_version, source).
    """
    sch_path = Path(sch_path).expanduser().resolve()
    if not sch_path.is_file():
        raise FileNotFoundError(sch_path)
    if output_json is None:
        output_json = sch_path.with_suffix(".erc.json")
    output_json = Path(output_json).expanduser().resolve()

    cli = _ensure_cli()
    sev_flag = f"--severity-{severity}"
    cmd = [
        str(cli), "sch", "erc",
        "--format", "json",
        sev_flag,
        "-o", str(output_json),
        str(sch_path),
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as e:
        raise KicadCliError(f"kicad-cli sch erc timed out after {timeout}s") from e
    if r.returncode != 0 or not output_json.is_file():
        raise KicadCliError(
            f"kicad-cli sch erc failed (rc={r.returncode}). "
            f"stderr: {r.stderr[-300:]}"
        )

    data = json.loads(output_json.read_text())
    return _shape_erc(data, output_json)


def _shape_erc(data: dict, raw_path: Path) -> dict:
    violations = data.get("violations") or []
    counts = _summarize_violations(violations)
    return {
        "kind": "erc",
        "source": data.get("source", ""),
        "kicad_version": data.get("kicad_version", ""),
        "date": data.get("date", ""),
        "errors": counts.get("error", 0),
        "warnings": counts.get("warning", 0),
        "exclusions": counts.get("exclusion", 0),
        "total_violations": sum(counts.values()),
        "violations": [_shape_violation(v) for v in violations],
        "raw_path": str(raw_path),
    }


# --------------------------------------------------------------------------- #
# DRC
# --------------------------------------------------------------------------- #


def run_drc(
    pcb_path: Path,
    *,
    output_json: Path | None = None,
    severity: str = "all",
    schematic_parity: bool = True,
    all_track_errors: bool = False,
    timeout: float = 120.0,
) -> dict:
    """Run `kicad-cli pcb drc --format json` and parse the report.

    Returns errors/warnings counts, violations, unconnected_items, and
    (when `schematic_parity=True`) parity findings between PCB and schematic.
    """
    pcb_path = Path(pcb_path).expanduser().resolve()
    if not pcb_path.is_file():
        raise FileNotFoundError(pcb_path)
    if output_json is None:
        output_json = pcb_path.with_suffix(".drc.json")
    output_json = Path(output_json).expanduser().resolve()

    cli = _ensure_cli()
    cmd = [
        str(cli), "pcb", "drc",
        "--format", "json",
        f"--severity-{severity}",
        "-o", str(output_json),
        str(pcb_path),
    ]
    if schematic_parity:
        cmd.insert(-1, "--schematic-parity")
    if all_track_errors:
        cmd.insert(-1, "--all-track-errors")

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as e:
        raise KicadCliError(f"kicad-cli pcb drc timed out after {timeout}s") from e
    if r.returncode != 0 or not output_json.is_file():
        raise KicadCliError(
            f"kicad-cli pcb drc failed (rc={r.returncode}). "
            f"stderr: {r.stderr[-300:]}"
        )

    data = json.loads(output_json.read_text())
    return _shape_drc(data, output_json)


# --------------------------------------------------------------------------- #
# Manufacturing exports
# --------------------------------------------------------------------------- #


def export_gerbers(
    pcb_path: Path,
    output_dir: Path,
    *,
    layers: Iterable[str] | None = None,
    timeout: float = 120.0,
) -> dict:
    """Run `kicad-cli pcb export gerbers`. Writes one file per layer + .gbrjob.

    Default layers (when `layers=None`) follow the project's plot settings.
    """
    pcb_path = Path(pcb_path).expanduser().resolve()
    output_dir = Path(output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    args = ["pcb", "export", "gerbers", "-o", str(output_dir)]
    if layers:
        args += ["--layers", ",".join(layers)]
    args.append(str(pcb_path))

    before = set(_list_files(output_dir))
    _run(args, timeout=timeout)
    after = set(_list_files(output_dir))
    written = sorted(after - before)
    return {
        "kind": "gerbers",
        "output_dir": str(output_dir),
        "files": written,
        "file_count": len(written),
    }


def export_drill(
    pcb_path: Path,
    output_dir: Path,
    *,
    drill_format: str = "excellon",
    generate_map: bool = True,
    map_format: str = "pdf",
    excellon_separate_th: bool = True,
    timeout: float = 60.0,
) -> dict:
    """Run `kicad-cli pcb export drill`."""
    pcb_path = Path(pcb_path).expanduser().resolve()
    output_dir = Path(output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    args = ["pcb", "export", "drill",
            "-o", str(output_dir),
            "--format", drill_format]
    if generate_map:
        args += ["--generate-map", "--map-format", map_format]
    if excellon_separate_th:
        args.append("--excellon-separate-th")
    args.append(str(pcb_path))

    before = set(_list_files(output_dir))
    _run(args, timeout=timeout)
    after = set(_list_files(output_dir))
    written = sorted(after - before)
    return {
        "kind": "drill",
        "output_dir": str(output_dir),
        "files": written,
        "file_count": len(written),
    }


def export_pos(
    pcb_path: Path,
    output_path: Path,
    *,
    side: str = "both",
    fmt: str = "csv",
    units: str = "mm",
    smd_only: bool = False,
    exclude_dnp: bool = False,
    timeout: float = 60.0,
) -> dict:
    """Run `kicad-cli pcb export pos`."""
    if side not in ("front", "back", "both"):
        raise KicadCliError(f"side must be front/back/both (got {side!r})")
    if fmt not in ("ascii", "csv", "gerber"):
        raise KicadCliError(f"format must be ascii/csv/gerber (got {fmt!r})")

    pcb_path = Path(pcb_path).expanduser().resolve()
    output_path = Path(output_path).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    args = ["pcb", "export", "pos",
            "-o", str(output_path),
            "--side", side,
            "--format", fmt,
            "--units", units]
    if smd_only:
        args.append("--smd-only")
    if exclude_dnp:
        args.append("--exclude-dnp")
    args.append(str(pcb_path))

    _run(args, timeout=timeout)
    if not output_path.is_file():
        raise KicadCliError(f"pos file was not created at {output_path}")
    return {
        "kind": "pos",
        "output_path": str(output_path),
        "size_bytes": output_path.stat().st_size,
        "side": side,
        "format": fmt,
    }


def export_bom(
    sch_path: Path,
    output_path: Path,
    *,
    fields: str | None = None,
    labels: str | None = None,
    group_by: str | None = "Value",
    sort_field: str = "Reference",
    exclude_dnp: bool = False,
    field_delimiter: str = ",",
    timeout: float = 60.0,
) -> dict:
    """Run `kicad-cli sch export bom`. Writes a CSV (default delimiter)."""
    sch_path = Path(sch_path).expanduser().resolve()
    output_path = Path(output_path).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    args = ["sch", "export", "bom",
            "-o", str(output_path),
            "--field-delimiter", field_delimiter]
    if fields:
        args += ["--fields", fields]
    if labels:
        args += ["--labels", labels]
    if group_by:
        args += ["--group-by", group_by]
    if sort_field:
        args += ["--sort-field", sort_field]
    if exclude_dnp:
        args.append("--exclude-dnp")
    args.append(str(sch_path))

    _run(args, timeout=timeout)
    if not output_path.is_file():
        raise KicadCliError(f"BOM was not created at {output_path}")
    text = output_path.read_text(encoding="utf-8", errors="replace")
    line_count = text.count("\n")
    return {
        "kind": "bom",
        "output_path": str(output_path),
        "size_bytes": output_path.stat().st_size,
        "line_count": line_count,
        "row_count": max(0, line_count - 1),  # minus header
    }


def export_netlist(
    sch_path: Path,
    output_path: Path,
    *,
    fmt: str = "kicadsexpr",
    timeout: float = 60.0,
) -> dict:
    """Run `kicad-cli sch export netlist`."""
    valid = {"kicadsexpr", "kicadxml", "cadstar", "orcadpcb2",
             "spice", "spicemodel", "pads", "allegro"}
    if fmt not in valid:
        raise KicadCliError(f"format must be one of {sorted(valid)} (got {fmt!r})")

    sch_path = Path(sch_path).expanduser().resolve()
    output_path = Path(output_path).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    args = ["sch", "export", "netlist",
            "-o", str(output_path),
            "--format", fmt,
            str(sch_path)]
    _run(args, timeout=timeout)
    if not output_path.is_file():
        raise KicadCliError(f"netlist was not created at {output_path}")
    return {
        "kind": "netlist",
        "output_path": str(output_path),
        "size_bytes": output_path.stat().st_size,
        "format": fmt,
    }


def render_pcb(
    pcb_path: Path,
    output_path: Path,
    *,
    side: str = "top",
    width: int = 1600,
    height: int = 900,
    quality: str = "basic",
    rotate: str | None = None,
    perspective: bool = False,
    timeout: float = 180.0,
) -> dict:
    """Run `kicad-cli pcb render` to produce a PNG/JPEG.

    `side`: top, bottom, left, right, front, back
    `quality`: basic | high (high is much slower)
    `rotate`: 'X,Y,Z' degrees, e.g. '-30,0,45' for isometric
    """
    if side not in ("top", "bottom", "left", "right", "front", "back"):
        raise KicadCliError(f"unsupported side {side!r}")
    pcb_path = Path(pcb_path).expanduser().resolve()
    output_path = Path(output_path).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    args = ["pcb", "render",
            "-o", str(output_path),
            "--side", side,
            "--width", str(width),
            "--height", str(height),
            "--quality", quality]
    if perspective:
        args.append("--perspective")
    if rotate:
        args += ["--rotate", rotate]
    args.append(str(pcb_path))

    _run(args, timeout=timeout)
    if not output_path.is_file():
        raise KicadCliError(f"render output not created at {output_path}")
    return {
        "kind": "render",
        "output_path": str(output_path),
        "size_bytes": output_path.stat().st_size,
        "side": side,
        "dimensions": [width, height],
        "quality": quality,
    }


def export_pcb_svg(
    pcb_path: Path,
    output_dir: Path,
    *,
    layers: Iterable[str] | None = None,
    fit_page_to_board: bool = True,
    black_and_white: bool = False,
    timeout: float = 60.0,
) -> dict:
    """Run `kicad-cli pcb export svg`. By default writes one SVG per layer."""
    pcb_path = Path(pcb_path).expanduser().resolve()
    output_dir = Path(output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    args = ["pcb", "export", "svg",
            "-o", str(output_dir),
            "--mode-multi"]
    if layers:
        args += ["--layers", ",".join(layers)]
    if fit_page_to_board:
        args.append("--fit-page-to-board")
    if black_and_white:
        args.append("--black-and-white")
    args.append(str(pcb_path))

    before = set(_list_files(output_dir))
    _run(args, timeout=timeout)
    after = set(_list_files(output_dir))
    written = sorted(after - before)
    return {
        "kind": "svg",
        "output_dir": str(output_dir),
        "files": written,
        "file_count": len(written),
    }


def _shape_drc(data: dict, raw_path: Path) -> dict:
    violations = data.get("violations") or []
    parity = data.get("schematic_parity") or []
    unconnected = data.get("unconnected_items") or []
    counts = _summarize_violations(violations)
    return {
        "kind": "drc",
        "source": data.get("source", ""),
        "kicad_version": data.get("kicad_version", ""),
        "date": data.get("date", ""),
        "errors": counts.get("error", 0),
        "warnings": counts.get("warning", 0),
        "exclusions": counts.get("exclusion", 0),
        "total_violations": sum(counts.values()),
        "unconnected_items_count": len(unconnected),
        "schematic_parity_count": len(parity),
        "violations": [_shape_violation(v) for v in violations],
        "unconnected_items": [_shape_violation(v) for v in unconnected],
        "schematic_parity": [_shape_violation(v) for v in parity],
        "raw_path": str(raw_path),
    }
