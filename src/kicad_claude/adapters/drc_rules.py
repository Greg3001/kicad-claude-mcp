"""Read and write `.kicad_dru` custom DRC rule files.

The DRU file is plain text, s-expression-flavoured. We keep rule operations
at the textual level — generating fresh content on every write — and use
sexpdata only for reading existing rules so we can list and remove them.

File layout::

    (version 1)

    (rule "Power clearance"
        (constraint clearance (min 0.5mm))
        (condition "A.NetClass == 'Power' || B.NetClass == 'Power'")
        (severity error)
    )

Common constraint types::

    clearance, hole_clearance, silk_clearance, physical_clearance,
    physical_hole_clearance, annular_width,
    track_width, via_diameter, via_drill, hole_size,
    diff_pair_gap, diff_pair_uncoupled, length, skew,
    text_height, text_thickness, disallow

Severities: error | warning | info | ignore
"""

from __future__ import annotations

import logging
from pathlib import Path

import sexpdata

from kicad_claude.adapters.sch_io import find_child, find_children, is_call

logger = logging.getLogger("kicad-claude.adapters.drc_rules")

VALID_CONSTRAINTS = frozenset({
    "clearance", "hole_clearance", "silk_clearance",
    "physical_clearance", "physical_hole_clearance",
    "annular_width",
    "track_width", "via_diameter", "via_drill",
    "hole_size", "track_segment_length",
    "diff_pair_gap", "diff_pair_uncoupled",
    "length", "skew",
    "text_height", "text_thickness",
    "min_resolved_spokes", "zone_connection",
    "disallow", "courtyard_clearance",
    "edge_clearance",
})

VALID_SEVERITIES = frozenset({"error", "warning", "info", "ignore"})


# --------------------------------------------------------------------------- #
# Read
# --------------------------------------------------------------------------- #


def read_rules(path: Path) -> list[dict]:
    """Parse a `.kicad_dru` file and return a list of rule dicts.

    If the file doesn't exist or has no version header, returns []. Best-effort
    parser — preserves rule names, constraints, conditions, severity. Other
    arbitrary fields are ignored.
    """
    path = Path(path)
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8")
    # The DRU format isn't a single s-expression — it's multiple top-level
    # forms. Wrap in parens so sexpdata can parse the whole document.
    try:
        wrapped = "(" + text + ")"
        forms = sexpdata.loads(wrapped)
    except Exception as e:  # noqa: BLE001
        logger.warning("failed to parse %s: %s", path, e)
        return []

    rules: list[dict] = []
    for form in forms:
        if not is_call(form, "rule"):
            continue
        if len(form) < 2 or not isinstance(form[1], str):
            continue
        name = form[1]
        rule = {"name": name, "constraint_type": "", "severity": "error"}
        for child in form[2:]:
            if not isinstance(child, list) or not child:
                continue
            head = sexpdata.Symbol if False else None
            head_name = (
                str(child[0]) if isinstance(child[0], sexpdata.Symbol) else None
            )
            if head_name == "constraint" and len(child) >= 2:
                ctype = str(child[1])
                rule["constraint_type"] = ctype
                # Pull min/opt/max
                for clause in child[2:]:
                    if not (isinstance(clause, list) and len(clause) >= 2):
                        continue
                    chead = (
                        str(clause[0])
                        if isinstance(clause[0], sexpdata.Symbol)
                        else None
                    )
                    if chead in ("min", "opt", "max"):
                        rule[f"{chead}_value"] = _parse_dim(clause[1])
            elif head_name == "condition" and len(child) >= 2:
                rule["condition"] = (
                    child[1] if isinstance(child[1], str) else ""
                )
            elif head_name == "severity" and len(child) >= 2:
                sev = (
                    str(child[1])
                    if isinstance(child[1], sexpdata.Symbol)
                    else child[1]
                )
                rule["severity"] = str(sev)
            elif head_name == "layer" and len(child) >= 2:
                rule["layer"] = (
                    str(child[1])
                    if isinstance(child[1], sexpdata.Symbol)
                    else child[1]
                )
        rules.append(rule)
    return rules


def _parse_dim(value) -> str:
    """Coerce a parsed dimension token (e.g. '0.5mm') to a plain Python str.

    sexpdata reads `0.5mm` as a `Symbol` (subclass of str but with a custom
    `__eq__` that only compares Symbol→Symbol). We want plain `str` so
    callers can compare with `==` against literal strings.
    """
    if isinstance(value, sexpdata.Symbol):
        return str(value)
    if isinstance(value, str):
        return str(value)  # plain Python str, no Symbol leakage
    return str(value)


# --------------------------------------------------------------------------- #
# Write
# --------------------------------------------------------------------------- #


def _format_value_clauses(rule: dict) -> str:
    parts = []
    for key in ("min_value", "opt_value", "max_value"):
        if key in rule and rule[key] is not None:
            kw = key.split("_")[0]
            v = rule[key]
            if isinstance(v, (int, float)):
                v = f"{v}mm"
            parts.append(f"({kw} {v})")
    return " ".join(parts)


def render_rule(rule: dict) -> str:
    """Render one rule dict as the `(rule ...)` text block."""
    name = rule["name"]
    ctype = rule["constraint_type"]
    severity = rule.get("severity", "error")
    cond = rule.get("condition", "")
    layer = rule.get("layer", "")

    value_clauses = _format_value_clauses(rule)
    if value_clauses:
        constraint_line = f'    (constraint {ctype} {value_clauses})'
    else:
        constraint_line = f'    (constraint {ctype})'

    lines = [f'(rule "{name}"', constraint_line]
    if cond:
        # Escape internal quotes minimally
        safe = cond.replace('\\', '\\\\').replace('"', '\\"')
        lines.append(f'    (condition "{safe}")')
    if layer:
        lines.append(f'    (layer "{layer}")')
    lines.append(f'    (severity {severity})')
    lines.append(")")
    return "\n".join(lines)


def write_rules(path: Path, rules: list[dict]) -> Path:
    """Overwrite `path` with `(version 1)` + every rule serialized."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    blocks = ["(version 1)", ""]
    for rule in rules:
        blocks.append(render_rule(rule))
        blocks.append("")
    path.write_text("\n".join(blocks).rstrip() + "\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


def validate_rule(rule: dict) -> None:
    """Raise ValueError on invalid rule dict."""
    if not rule.get("name"):
        raise ValueError("rule must have a non-empty name")
    ctype = rule.get("constraint_type", "")
    if not ctype:
        raise ValueError(f"rule {rule['name']!r} missing constraint_type")
    if ctype not in VALID_CONSTRAINTS:
        raise ValueError(
            f"unknown constraint_type {ctype!r}. Valid: {sorted(VALID_CONSTRAINTS)}"
        )
    sev = rule.get("severity", "error")
    if sev not in VALID_SEVERITIES:
        raise ValueError(
            f"severity must be one of {sorted(VALID_SEVERITIES)} (got {sev!r})"
        )
    # Some constraints don't need a min — `disallow` for instance has its own
    # syntax. We don't enforce min presence for that reason.
