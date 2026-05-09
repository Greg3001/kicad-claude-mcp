"""Bridge to KiCAD's bundled Python interpreter for `pcbnew` API access.

KiCAD ships its own Python 3.9 with the `pcbnew` SWIG bindings. The host
Python (this MCP server, 3.10+) cannot import `pcbnew` directly because the
bindings are compiled for KiCAD's exact Python version. Instead we shell out
to KiCAD's Python and run a tiny script.

This is the only way (without IPC API in KiCAD 11) to use KiCAD's native
Specctra DSN export and SES import — `kicad-cli` v10 doesn't expose them.
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger("kicad-claude.adapters.kicad_python")


class KicadPythonError(RuntimeError):
    """KiCAD's bundled Python failed (not found, returned non-zero, etc.)."""


def find_kicad_python() -> Path | None:
    """Locate KiCAD's bundled Python interpreter.

    Resolution order:
    1. KICAD_PYTHON env override
    2. macOS: /Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/.../bin/python3.X
    3. Linux: /usr/lib/kicad/bin/python3 (rare; most distros use system Python)
    4. Windows: %ProgramFiles%\\KiCad\\<ver>\\bin\\python.exe
    """
    override = os.environ.get("KICAD_PYTHON")
    if override and Path(override).is_file():
        return Path(override)

    sys_name = platform.system()
    if sys_name == "Darwin":
        framework = Path("/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions")
        if framework.is_dir():
            # Prefer "Current" symlink, else newest 3.x.
            current = framework / "Current" / "bin" / "python3"
            if current.is_file():
                return current
            for ver_dir in sorted(framework.iterdir(), reverse=True):
                if not ver_dir.is_dir():
                    continue
                p = ver_dir / "bin" / "python3"
                if p.is_file():
                    return p

    if sys_name == "Linux":
        candidate = Path("/usr/lib/kicad/bin/python3")
        if candidate.is_file():
            return candidate
        # Fallback to system python with pcbnew available.
        sys_python = shutil.which("python3")
        if sys_python:
            return Path(sys_python)

    if sys_name == "Windows":
        for ver in ("10", "9", "8"):
            cand = Path(rf"C:\Program Files\KiCad\{ver}\bin\python.exe")
            if cand.is_file():
                return cand

    return None


def _ensure_python() -> Path:
    p = find_kicad_python()
    if p is None:
        raise KicadPythonError(
            "couldn't find KiCAD's bundled Python (no `pcbnew` available). "
            "Set KICAD_PYTHON in .env to override."
        )
    return p


def _run_pcbnew_script(script: str, *args: str, timeout: float = 60.0) -> str:
    """Run `script` against KiCAD's Python; return stdout. Raises on failure."""
    py = _ensure_python()
    try:
        r = subprocess.run(
            [str(py), "-c", script, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise KicadPythonError(f"pcbnew script timed out after {timeout}s") from e
    if r.returncode != 0:
        raise KicadPythonError(
            f"pcbnew script failed (rc={r.returncode}): "
            f"stdout={r.stdout[-300:]} stderr={r.stderr[-300:]}"
        )
    return r.stdout


# --------------------------------------------------------------------------- #
# Public functions
# --------------------------------------------------------------------------- #

_EXPORT_DSN_SCRIPT = """
import sys, os
import pcbnew
pcb_path, dsn_path = sys.argv[1], sys.argv[2]
board = pcbnew.LoadBoard(pcb_path)
if not board:
    print("LoadBoard returned None", file=sys.stderr); sys.exit(1)
ok = pcbnew.ExportSpecctraDSN(board, dsn_path)
print("EXPORT_OK" if ok else "EXPORT_FAIL")
sys.exit(0 if ok else 1)
"""


_IMPORT_SES_SCRIPT = """
import sys, os
import pcbnew
pcb_path, ses_path = sys.argv[1], sys.argv[2]
board = pcbnew.LoadBoard(pcb_path)
if not board:
    print("LoadBoard returned None", file=sys.stderr); sys.exit(1)
ok = pcbnew.ImportSpecctraSES(board, ses_path)
if not ok:
    print("ImportSpecctraSES returned False", file=sys.stderr); sys.exit(1)
saved = pcbnew.SaveBoard(pcb_path, board)
print("IMPORT_OK" if saved else "IMPORT_FAIL")
sys.exit(0 if saved else 1)
"""


def export_dsn(pcb_path: Path, dsn_path: Path, timeout: float = 60.0) -> Path:
    """Export the .kicad_pcb to Specctra DSN at `dsn_path`.

    Uses KiCAD's bundled Python (`pcbnew.ExportSpecctraDSN`).
    """
    pcb_path = Path(pcb_path).expanduser().resolve()
    dsn_path = Path(dsn_path).expanduser().resolve()
    if not pcb_path.is_file():
        raise FileNotFoundError(pcb_path)
    out = _run_pcbnew_script(
        _EXPORT_DSN_SCRIPT, str(pcb_path), str(dsn_path), timeout=timeout
    )
    if not dsn_path.is_file():
        raise KicadPythonError(
            f"DSN file not produced at {dsn_path}; pcbnew said: {out.strip()}"
        )
    return dsn_path


def import_ses(pcb_path: Path, ses_path: Path, timeout: float = 60.0) -> Path:
    """Apply a Freerouting .ses session back into the .kicad_pcb (in-place)."""
    pcb_path = Path(pcb_path).expanduser().resolve()
    ses_path = Path(ses_path).expanduser().resolve()
    if not pcb_path.is_file():
        raise FileNotFoundError(pcb_path)
    if not ses_path.is_file():
        raise FileNotFoundError(ses_path)
    _run_pcbnew_script(
        _IMPORT_SES_SCRIPT, str(pcb_path), str(ses_path), timeout=timeout
    )
    return pcb_path


# --------------------------------------------------------------------------- #
# Apply netlist to PCB (poor man's "Update PCB from Schematic")
# --------------------------------------------------------------------------- #

_APPLY_NETLIST_SCRIPT = r"""
import sys
import json
import xml.etree.ElementTree as ET
import pcbnew

pcb_path = sys.argv[1]
xml_path = sys.argv[2]

# Parse the kicadxml netlist: ref -> {pin -> net_name}
tree = ET.parse(xml_path)
root = tree.getroot()
ref_to_pad_nets = {}
all_nets = set()
for net in root.findall("nets/net"):
    net_name = net.get("name") or ""
    if not net_name:
        continue
    all_nets.add(net_name)
    for node in net.findall("node"):
        ref = node.get("ref")
        pin = node.get("pin")
        if ref and pin:
            ref_to_pad_nets.setdefault(ref, {})[pin] = net_name

# Load the board
board = pcbnew.LoadBoard(pcb_path)

# Make sure every net from the schematic exists on the board.
# `board.FindNet(name)` returns the NETINFO_ITEM or None — that's the
# easiest existence check. SWIG's NETNAMES_MAP isn't a real dict.
added_nets = 0
for net_name in sorted(all_nets):
    if board.FindNet(net_name) is None:
        ni = pcbnew.NETINFO_ITEM(board, net_name)
        board.Add(ni)
        added_nets += 1

# Walk footprints and update pad net assignments
fps = list(board.GetFootprints())
schematic_refs = set(ref_to_pad_nets.keys())
pcb_refs = {fp.GetReference() for fp in fps}
missing_in_pcb = sorted(schematic_refs - pcb_refs)

pad_changes = 0
matched = 0
for fp in fps:
    ref = fp.GetReference()
    if ref not in ref_to_pad_nets:
        continue
    matched += 1
    pad_nets = ref_to_pad_nets[ref]
    for pad in fp.Pads():
        num = pad.GetNumber()
        if num in pad_nets:
            net_obj = board.FindNet(pad_nets[num])
            if net_obj is not None:
                pad.SetNet(net_obj)
                pad_changes += 1

pcbnew.SaveBoard(pcb_path, board)

result = {
    "added_nets": added_nets,
    "matched_footprints": matched,
    "missing_in_pcb": missing_in_pcb,
    "pad_assignments_made": pad_changes,
    "schematic_references": len(schematic_refs),
    "pcb_references": len(pcb_refs),
    "schematic_nets": len(all_nets),
}
print("RESULT_JSON:" + json.dumps(result))
"""


def apply_netlist(pcb_path: Path, netlist_xml_path: Path, timeout: float = 90.0) -> dict:
    """Apply a kicadxml netlist to the PCB: assign nets to pads, add missing nets.

    The netlist must be in `kicadxml` format (use `export_netlist(format="kicadxml")`
    to produce it). Footprints in the schematic that aren't on the PCB are
    listed as `missing_in_pcb` — call `add_footprint` for each, then re-run.
    """
    pcb_path = Path(pcb_path).expanduser().resolve()
    netlist_xml_path = Path(netlist_xml_path).expanduser().resolve()
    if not pcb_path.is_file():
        raise FileNotFoundError(pcb_path)
    if not netlist_xml_path.is_file():
        raise FileNotFoundError(netlist_xml_path)

    out = _run_pcbnew_script(
        _APPLY_NETLIST_SCRIPT, str(pcb_path), str(netlist_xml_path), timeout=timeout
    )
    # Parse the trailing RESULT_JSON: line.
    result_line = next(
        (line for line in reversed(out.splitlines()) if line.startswith("RESULT_JSON:")),
        None,
    )
    if result_line is None:
        raise KicadPythonError(f"apply_netlist script produced no result: {out[-300:]}")
    import json
    return json.loads(result_line[len("RESULT_JSON:"):])
