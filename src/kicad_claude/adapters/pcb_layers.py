"""Generate `(layers ...)` and `(setup (stackup ...) ...)` blocks for KiCAD PCBs.

Discovered (empirically against KiCAD 10.0.1) numbering scheme for the copper
layers in the file format:

    F.Cu      = 0
    B.Cu      = 2          (constant — does NOT shift with layer count)
    In{k}.Cu  = 2*(k + 1)  for k = 1..N-2

So for a 4-copper board: F.Cu=0, In1.Cu=4, In2.Cu=6, B.Cu=2.

The non-copper user layers (silkscreen, mask, paste, etc.) keep fixed
numbers regardless of total copper count.

Stackup follows a uniform pattern: thin dielectric between every pair of
adjacent copper layers, sized so total board thickness ≈ 1.6 mm.
"""

from __future__ import annotations

from typing import Any

from kicad_claude.adapters.sch_io import sym

MAX_COPPER_LAYERS = 32  # KiCAD's hard cap; we usually limit to 12 in practice
TARGET_BOARD_THICKNESS_MM = 1.6
COPPER_THICKNESS_MM = 0.035


# Fixed numbers for non-copper user layers — confirmed by inspecting outputs
# of pcbnew.SetCopperLayerCount() across N ∈ {2, 4, 6, 12}.
_USER_LAYERS: list[tuple[int, str, str, str | None]] = [
    (9, "F.Adhes", "user", "F.Adhesive"),
    (11, "B.Adhes", "user", "B.Adhesive"),
    (13, "F.Paste", "user", None),
    (15, "B.Paste", "user", None),
    (5, "F.SilkS", "user", "F.Silkscreen"),
    (7, "B.SilkS", "user", "B.Silkscreen"),
    (1, "F.Mask", "user", None),
    (3, "B.Mask", "user", None),
    (17, "Dwgs.User", "user", "User.Drawings"),
    (19, "Cmts.User", "user", "User.Comments"),
    (21, "Eco1.User", "user", "User.Eco1"),
    (23, "Eco2.User", "user", "User.Eco2"),
    (25, "Edge.Cuts", "user", None),
    (27, "Margin", "user", None),
    (31, "F.CrtYd", "user", "F.Courtyard"),
    (29, "B.CrtYd", "user", "B.Courtyard"),
    (35, "F.Fab", "user", None),
    (33, "B.Fab", "user", None),
]


def copper_layer_names(n: int) -> list[str]:
    """Return ordered list of copper layer names: [F.Cu, In1.Cu, ..., B.Cu]."""
    if not (2 <= n <= MAX_COPPER_LAYERS):
        raise ValueError(
            f"copper layer count must be 2-{MAX_COPPER_LAYERS} (got {n})"
        )
    if n % 2:
        raise ValueError(f"copper layer count must be even (got {n})")
    if n == 2:
        return ["F.Cu", "B.Cu"]
    inner = [f"In{k}.Cu" for k in range(1, n - 1)]
    return ["F.Cu", *inner, "B.Cu"]


def copper_layer_id(name: str) -> int:
    """Return the file-format integer ID for a copper layer name."""
    if name == "F.Cu":
        return 0
    if name == "B.Cu":
        return 2
    if name.startswith("In") and name.endswith(".Cu"):
        try:
            k = int(name[2:-3])
            return 2 * (k + 1)
        except ValueError:
            pass
    raise ValueError(f"not a copper layer name: {name!r}")


def build_layers_block(n: int) -> list:
    """Return the `(layers ...)` s-expression node for `n` copper layers."""
    rows: list[Any] = [sym("layers")]

    # Copper layers: ORDER in the file is F.Cu, In1.Cu..InN-2.Cu, B.Cu
    # (matches what pcbnew.SetCopperLayerCount writes).
    for name in copper_layer_names(n):
        rows.append([copper_layer_id(name), name, sym("signal")])

    # User layers: in the same order pcbnew uses.
    for layer_id, name, kind, alias in _USER_LAYERS:
        row = [layer_id, name, sym(kind)]
        if alias:
            row.append(alias)
        rows.append(row)
    return rows


def _dielectric_thickness(n: int) -> float:
    """Pick a dielectric thickness so the board ends up ~1.6 mm regardless of N."""
    n_dielectrics = n - 1
    return round(
        (TARGET_BOARD_THICKNESS_MM - n * COPPER_THICKNESS_MM) / n_dielectrics, 4
    )


def build_stackup_block(n: int) -> list:
    """Return the `(stackup ...)` s-expression node for `n` copper layers.

    Uniform FR4 dielectrics, alternating with copper. Total ≈ 1.6 mm.
    The user can edit per-layer materials in KiCAD's Board Setup UI later.
    """
    if n < 2 or n % 2:
        raise ValueError(f"copper layer count must be even ≥ 2 (got {n})")

    diel_t = _dielectric_thickness(n)
    out: list[Any] = [sym("stackup")]

    # Top side (always present)
    out.append([sym("layer"), "F.SilkS", [sym("type"), "Top Silk Screen"]])
    out.append([sym("layer"), "F.Paste", [sym("type"), "Top Solder Paste"]])
    out.append(
        [
            sym("layer"),
            "F.Mask",
            [sym("type"), "Top Solder Mask"],
            [sym("color"), "Green"],
            [sym("thickness"), 0.01],
        ]
    )

    # Copper / dielectric chain
    copper_names = copper_layer_names(n)
    for i, cname in enumerate(copper_names):
        out.append(
            [
                sym("layer"),
                cname,
                [sym("type"), "copper"],
                [sym("thickness"), COPPER_THICKNESS_MM],
            ]
        )
        if i < len(copper_names) - 1:
            out.append(
                [
                    sym("layer"),
                    f"dielectric {i + 1}",
                    [sym("type"), "core" if i == 0 and n == 2 else "prepreg"],
                    [sym("thickness"), diel_t],
                    [sym("material"), "FR4"],
                    [sym("epsilon_r"), 4.5],
                    [sym("loss_tangent"), 0.02],
                ]
            )

    # Bottom side
    out.append(
        [
            sym("layer"),
            "B.Mask",
            [sym("type"), "Bottom Solder Mask"],
            [sym("color"), "Green"],
            [sym("thickness"), 0.01],
        ]
    )
    out.append([sym("layer"), "B.Paste", [sym("type"), "Bottom Solder Paste"]])
    out.append([sym("layer"), "B.SilkS", [sym("type"), "Bottom Silk Screen"]])

    out.append([sym("copper_finish"), "None"])
    out.append([sym("dielectric_constraints"), sym("no")])
    return out
