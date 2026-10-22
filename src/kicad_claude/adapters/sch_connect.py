"""Wire pins to nets in bulk: stub wire + label (or no-connect) per pin.

Why this exists: capturing a 480-pin SOM connector one `add_wire` +
`add_label` call at a time is ~1000 tool round trips, each re-parsing and
re-writing the whole sheet. `connect_pins` plans every connection first (so a
typo in pin 417 changes nothing) and applies them in a single pass.

Also home of the global label, which the editor lacked: a local label is
sheet-scoped (`/power/GND`), so nets that cross sheets need a global or
hierarchical label.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass

from kicad_claude.adapters import sch_editor as ed
from kicad_claude.adapters.sch_io import sym
from kicad_claude.utils.geometry import SCH_GRID_MM, mcp_to_kicad_xy, round_mm

DEFAULT_STUB_MM = 5.08  # two grid steps: room for the label, clear of the pin
SCOPES = ("local", "global", "hierarchical")
_ORIENTATION_BY_ANGLE = {0: "right", 90: "up", 180: "left", 270: "down"}
_GLOBAL_SHAPES = {"input", "output", "bidirectional", "tri_state", "passive"}


def add_global_label(
    tree: list,
    net_name: str,
    x_mm: float,
    y_mm: float,
    shape: str = "bidirectional",
    orientation: str = "right",
) -> list:
    """Append a `(global_label ...)`: one net name shared by every sheet."""
    if shape not in _GLOBAL_SHAPES:
        raise ValueError(f"shape must be one of {sorted(_GLOBAL_SHAPES)}")
    angle = {"right": 0, "up": 90, "left": 180, "down": 270}.get(orientation)
    if angle is None:
        raise ValueError("orientation must be one of ['right', 'up', 'left', 'down']")
    xk, yk = mcp_to_kicad_xy(x_mm, y_mm, ed.page_height_mm(tree))
    xk, yk = round_mm(xk), round_mm(yk)
    effects = [sym("effects"), [sym("font"), [sym("size"), 1.27, 1.27]], [sym("justify"), sym("left")]]
    node = [
        sym("global_label"),
        net_name,
        [sym("shape"), sym(shape)],
        [sym("at"), xk, yk, angle],
        [sym("fields_autoplaced"), sym("yes")],
        effects,
        [sym("uuid"), str(uuid.uuid4())],
        [
            sym("property"), "Intersheetrefs", "${INTERSHEET_REFS}",
            [sym("at"), xk, yk, 0],
            [sym("effects"), [sym("font"), [sym("size"), 1.27, 1.27]],
             [sym("justify"), sym("left")], [sym("hide"), sym("yes")]],
        ],
    ]
    tree.append(node)
    return node


@dataclass(frozen=True)
class PlannedPin:
    pin: str
    x: float
    y: float
    outward: int  # world angle pointing away from the body, 0/90/180/270
    net: str | None  # None => no-connect
    scope: str
    shape: str


def _outward(angle_into_body: float) -> int:
    """Direction a stub leaves a pin: the opposite of where the pin points."""
    out = int(round(angle_into_body + 180)) % 360
    if out not in _ORIENTATION_BY_ANGLE:
        raise ValueError(f"pin angle {angle_into_body} is not a multiple of 90")
    return out


def _stub_end(x: float, y: float, outward: int, length: float) -> tuple[float, float]:
    rad = math.radians(outward)
    return round(x + length * round(math.cos(rad)), 4), round(y + length * round(math.sin(rad)), 4)


def _check_item(item: dict, default_scope: str, default_shape: str) -> tuple[str | None, str, str]:
    net = None if item.get("nc") else item.get("net")
    if not item.get("nc") and not net:
        raise ValueError(f"pin {item.get('pin')!r}: give `net` or `nc: true`")
    scope = item.get("scope", default_scope)
    if scope not in SCOPES:
        raise ValueError(f"pin {item.get('pin')!r}: scope must be one of {SCOPES}")
    return net, scope, item.get("shape", default_shape)


def plan_connections(
    tree: list,
    reference: str,
    connections: list[dict],
    *,
    unit: int | None = None,
    default_scope: str = "local",
    default_shape: str = "bidirectional",
) -> list[PlannedPin]:
    """Resolve every connection to coordinates; raises on the first bad one."""
    pins = ed.list_pins_for_symbol(tree, reference, unit)
    by_number = {}
    for p in pins:  # first placed unit wins for pins shared between units
        by_number.setdefault(p["number"], p)
    seen: set[str] = set()
    plan = []
    for item in connections:
        number = str(item.get("pin", ""))
        if number not in by_number:
            raise KeyError(
                f"pin {number!r} is not on a placed unit of {reference!r}"
                f"{'' if unit is None else f' unit {unit}'}"
            )
        if number in seen:
            raise ValueError(f"pin {number!r} listed twice")
        seen.add(number)
        net, scope, shape = _check_item(item, default_scope, default_shape)
        pin = by_number[number]
        x, y = pin["position_mm"]
        plan.append(PlannedPin(number, x, y, _outward(pin["angle"]), net, scope, shape))
    return plan


def _place_label(tree: list, p: PlannedPin, x: float, y: float) -> None:
    orientation = _ORIENTATION_BY_ANGLE[p.outward]
    if p.scope == "global":
        add_global_label(tree, p.net, x, y, p.shape, orientation)
    elif p.scope == "hierarchical":
        ed.add_hierarchical_label(
            tree, net_name=p.net, x_mm=x, y_mm=y, shape=p.shape, orientation=orientation
        )
    else:
        ed.add_label(tree, p.net, x, y, orientation)


def apply_plan(tree: list, plan: list[PlannedPin], stub_mm: float = DEFAULT_STUB_MM) -> dict:
    """Write the planned stubs/labels/no-connects into `tree`."""
    if stub_mm <= 0 or abs(stub_mm / SCH_GRID_MM - round(stub_mm / SCH_GRID_MM)) > 1e-6:
        raise ValueError(f"stub_mm must be a positive multiple of {SCH_GRID_MM}")
    labelled = no_connect = 0
    for p in plan:
        if p.net is None:
            ed.add_no_connect(tree, p.x, p.y)
            no_connect += 1
            continue
        ex, ey = _stub_end(p.x, p.y, p.outward, stub_mm)
        ed.add_wire(tree, p.x, p.y, ex, ey)
        _place_label(tree, p, ex, ey)
        labelled += 1
    return {"labelled": labelled, "no_connect": no_connect}
