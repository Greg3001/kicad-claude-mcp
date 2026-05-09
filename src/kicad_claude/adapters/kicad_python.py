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
