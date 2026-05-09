"""Mouser Search API V2 client.

Single-key auth via query string (`apiKey=<KEY>`). No OAuth, no caching needed.

Required environment:
    MOUSER_API_KEY
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger("kicad-claude.adapters.mouser")

DEFAULT_BASE = "https://api.mouser.com/api/v2"


class MouserError(RuntimeError):
    pass


def _api_key() -> str:
    key = os.environ.get("MOUSER_API_KEY")
    if not key:
        raise MouserError("MOUSER_API_KEY not set; configure it in .env")
    return key


def _base_url() -> str:
    return os.environ.get("MOUSER_API_BASE", DEFAULT_BASE).rstrip("/")


def _post(path: str, body: dict[str, Any]) -> Any:
    url = f"{_base_url()}{path}"
    try:
        r = httpx.post(
            url,
            params={"apiKey": _api_key()},
            json=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            timeout=20.0,
        )
    except httpx.HTTPError as e:
        raise MouserError(f"network error: {e}") from e
    if r.status_code >= 400:
        raise MouserError(f"POST {path} → {r.status_code}: {r.text[:300]}")
    payload = r.json()
    errs = payload.get("Errors") or []
    if errs:
        # Mouser returns 200 with an Errors array on auth failures.
        raise MouserError(f"Mouser API errors: {errs}")
    return payload


# --------------------------------------------------------------------------- #
# Public methods
# --------------------------------------------------------------------------- #


def search_part(mpn: str) -> list[dict]:
    """Look up by manufacturer part number. Returns up to ~50 simplified results."""
    payload = _post(
        "/search/partnumber",
        {
            "SearchByPartRequest": {
                "mouserPartNumber": mpn,
                "partSearchOptions": "1",  # 1 = "Begins with"; 2 = exact
            }
        },
    )
    parts = (payload.get("SearchResults") or {}).get("Parts") or []
    return [_summarize_part(p) for p in parts]


def search_keyword(query: str, records: int = 10) -> list[dict]:
    """Free-text keyword search."""
    payload = _post(
        "/search/keyword",
        {
            "SearchByKeywordRequest": {
                "keyword": query,
                "records": records,
                "startingRecord": 0,
                "searchOptions": "",
                "searchWithYourSignUpLanguage": "false",
            }
        },
    )
    parts = (payload.get("SearchResults") or {}).get("Parts") or []
    return [_summarize_part(p) for p in parts]


# --------------------------------------------------------------------------- #
# Response shaping
# --------------------------------------------------------------------------- #


def _summarize_part(p: dict) -> dict:
    # Mouser stock is a string like "5,432". Try to coerce.
    stock_raw = p.get("AvailabilityInStock") or "0"
    try:
        stock = int(str(stock_raw).replace(",", "").replace(".", ""))
    except ValueError:
        stock = 0

    price_breaks = p.get("PriceBreaks") or []
    unit_price = None
    currency = ""
    if price_breaks:
        first = price_breaks[0]
        # Mouser returns price as e.g. "0,420 €" with comma decimal in some locales
        raw = first.get("Price", "") or ""
        cleaned = raw.replace(",", ".")
        # Strip currency symbols
        for sym in ("€", "$", "£", "¥"):
            cleaned = cleaned.replace(sym, "")
        try:
            unit_price = float(cleaned.strip())
        except ValueError:
            unit_price = None
        currency = first.get("Currency", "")

    return {
        "source": "mouser",
        "mpn": p.get("ManufacturerPartNumber", ""),
        "manufacturer": p.get("Manufacturer", ""),
        "description": p.get("Description", ""),
        "stock": stock,
        "unit_price": unit_price,
        "currency": currency,
        "datasheet_url": p.get("DataSheetUrl", "") or "",
        "product_url": p.get("ProductDetailUrl", "") or "",
        "lead_time": p.get("LeadTime", ""),
    }
