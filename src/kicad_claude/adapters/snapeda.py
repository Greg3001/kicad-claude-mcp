"""SnapEDA helper.

SnapEDA does not expose a public, unauthenticated API for downloading ECAD
models. Their site requires login + cookie session, and download URLs rotate.
Anything we attempt to scrape is fragile by definition.

What we *can* do:
- Generate the canonical part-page URL so the user can click through.
- (Best effort) probe DigiKey's product detail for ECAD model links — those
  often point at SnapEDA / Ultra Librarian.

For Phase 4 we rely on the manual fallback flow:
    1. The user opens https://www.snapeda.com/parts/<MPN>
    2. Logs in, downloads the KiCad ZIP
    3. Drops it into ./vendor_parts/
    4. Calls `import_vendor_zip` to register it in the project

This keeps Phase 4 reliable without depending on third-party site internals.
"""

from __future__ import annotations

import logging
from urllib.parse import quote

logger = logging.getLogger("kicad-claude.adapters.snapeda")


def part_page_url(mpn: str, manufacturer: str | None = None) -> str:
    """Return the canonical SnapEDA part page URL.

    SnapEDA URLs follow `https://www.snapeda.com/parts/<MPN>/<MFR>/view-part/`
    when a manufacturer is provided, else `/search?q=<MPN>`.
    """
    if manufacturer:
        return f"https://www.snapeda.com/parts/{quote(mpn)}/{quote(manufacturer)}/view-part/"
    return f"https://www.snapeda.com/search?q={quote(mpn)}"


def manual_fallback_message(
    mpn: str, manufacturer: str | None = None, vendor_parts_dir: str | None = None
) -> str:
    """Build a clear instruction string for the user to download manually."""
    url = part_page_url(mpn, manufacturer)
    drop_dir = vendor_parts_dir or "./vendor_parts"
    return (
        f"No KiCad assets found locally for {mpn!r}. "
        f"To add it: 1) open {url} 2) log in and download the KiCad ZIP "
        f"3) save it to {drop_dir}/{mpn}.zip "
        f"4) call `import_vendor_zip` with that path."
    )
