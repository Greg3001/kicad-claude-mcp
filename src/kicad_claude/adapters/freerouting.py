"""Freerouting JAR runner.

Invokes `java -jar freerouting.jar -de input.dsn -do output.ses -mp <passes>`
with timeouts and parses progress lines from stdout.

Required environment:
    FREEROUTING_JAR     (path to freerouting-2.x.x.jar; default: ./third_party/freerouting.jar)
Optional:
    JAVA_BIN            (default: `java` on PATH)
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger("kicad-claude.adapters.freerouting")


class FreeroutingError(RuntimeError):
    pass


# Match the summary lines Freerouting 2.x prints, e.g.:
#   "INFO   Routing took 0:00:42 to complete"
#   "INFO   Total trace length: 287.5 mm"
#   "INFO   Total of 12 vias"
_PATTERNS = {
    "completion_pct": re.compile(r"(\d+(?:\.\d+)?)\s*% completed", re.IGNORECASE),
    "via_count": re.compile(r"(?:total of\s+)?(\d+)\s+vias", re.IGNORECASE),
    "trace_length_mm": re.compile(
        r"total\s+trace\s+length[:\s]+(\d+(?:\.\d+)?)\s*mm", re.IGNORECASE
    ),
    "duration": re.compile(r"routing took\s+(\d+:\d+:\d+)", re.IGNORECASE),
    "passes_done": re.compile(r"pass\s+(\d+)\s+complete", re.IGNORECASE),
}


def find_freerouting_jar() -> Path | None:
    """Resolve the Freerouting JAR path.

    Order: FREEROUTING_JAR env > <repo>/third_party/freerouting.jar.
    """
    env = os.environ.get("FREEROUTING_JAR")
    if env:
        p = Path(env).expanduser()
        if p.is_file():
            return p

    repo_root = Path(__file__).resolve().parents[3]
    candidate = repo_root / "third_party" / "freerouting.jar"
    if candidate.is_file():
        return candidate
    return None


def find_java() -> Path | None:
    custom = os.environ.get("JAVA_BIN")
    if custom and Path(custom).is_file():
        return Path(custom)
    java = shutil.which("java")
    return Path(java) if java else None


def _ensure_environment() -> tuple[Path, Path]:
    jar = find_freerouting_jar()
    if jar is None:
        raise FreeroutingError(
            "freerouting.jar not found. Set FREEROUTING_JAR or download it to "
            "third_party/freerouting.jar (https://github.com/freerouting/freerouting/releases)"
        )
    java = find_java()
    if java is None:
        raise FreeroutingError("`java` not on PATH; install Java 21+")
    return jar, java


def _parse_stats(combined_output: str) -> dict:
    """Pull whatever stats Freerouting prints into a dict. Best-effort.

    For progress fields (`completion_pct`, `passes_done`) we take the LAST
    match so the final state is reported. Totals (`via_count`,
    `trace_length_mm`) and `duration` use the first/only match.
    """
    stats: dict = {}
    last_match_keys = {"completion_pct", "passes_done"}

    for key, pattern in _PATTERNS.items():
        matches = pattern.findall(combined_output)
        if not matches:
            continue
        stats[key] = matches[-1] if key in last_match_keys else matches[0]

    # Coerce numbers where possible.
    for k in ("via_count", "passes_done"):
        if k in stats:
            try:
                stats[k] = int(stats[k])
            except ValueError:
                pass
    for k in ("completion_pct", "trace_length_mm"):
        if k in stats:
            try:
                stats[k] = float(stats[k])
            except ValueError:
                pass
    return stats


def route(
    dsn_path: Path,
    ses_path: Path,
    *,
    passes: int = 100,
    threads: int | None = None,
    timeout_seconds: float = 300.0,
    extra_args: list[str] | None = None,
) -> dict:
    """Run Freerouting on `dsn_path` and write the result to `ses_path`.

    Returns a dict with `returncode`, `stats`, and `stdout_tail`/`stderr_tail`.
    Times out hard at `timeout_seconds` (Freerouting can run forever on
    pathological boards).
    """
    jar, java = _ensure_environment()
    dsn_path = Path(dsn_path).expanduser().resolve()
    ses_path = Path(ses_path).expanduser().resolve()
    if not dsn_path.is_file():
        raise FileNotFoundError(dsn_path)

    cmd = [
        str(java),
        "-jar",
        str(jar),
        "-de",
        str(dsn_path),
        "-do",
        str(ses_path),
        "-mp",
        str(passes),
    ]
    if threads is not None:
        cmd += ["-mt", str(threads)]
    if extra_args:
        cmd += list(extra_args)

    logger.info("running freerouting: %s", " ".join(cmd))
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise FreeroutingError(
            f"Freerouting timed out after {timeout_seconds}s "
            f"(consider larger timeout, fewer passes, or simpler design)"
        ) from e

    combined = (r.stdout or "") + "\n" + (r.stderr or "")

    if not ses_path.is_file():
        # Even if rc == 0, no SES means routing didn't complete.
        raise FreeroutingError(
            f"Freerouting did not produce {ses_path}. "
            f"rc={r.returncode}, stderr tail: {r.stderr[-400:]}"
        )

    return {
        "returncode": r.returncode,
        "ses_path": str(ses_path),
        "stats": _parse_stats(combined),
        "stdout_tail": (r.stdout or "")[-1000:],
        "stderr_tail": (r.stderr or "")[-400:],
    }
