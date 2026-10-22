"""Find the power nets that ERC says nothing drives.

Why this exists: a board whose rails come from a connector or a module has no
output-type power pin on them, so ERC reports `power_pin_not_driven` for every
power symbol on the net. The cure is one PWR_FLAG per net. ERC text is
localised, so only the violation `type` and the `#PWR0001`-style reference
inside the item description are relied on.
"""

from __future__ import annotations

import re

_REF_RE = re.compile(r"(#[A-Za-z_]+\d+)")
UNDRIVEN_TYPE = "power_pin_not_driven"


def undriven_refs(erc_data: dict) -> list[tuple[str, str]]:
    """(sheet path, power-symbol reference) for each undriven power pin."""
    out: list[tuple[str, str]] = []
    for sheet in erc_data.get("sheets") or []:
        for v in sheet.get("violations") or []:
            if v.get("type") != UNDRIVEN_TYPE:
                continue
            for item in v.get("items") or []:
                m = _REF_RE.search(item.get("description", ""))
                if m:
                    out.append((sheet.get("path", "/"), m.group(1)))
                    break
    return out
