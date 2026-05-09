"""DigiKey Product Information API V4 client.

Uses OAuth2 client_credentials. The bearer token is cached on disk so we
don't reauth on every call (DigiKey expires it in ~30 min).

Required environment variables:
    DIGIKEY_CLIENT_ID
    DIGIKEY_CLIENT_SECRET
Optional:
    DIGIKEY_API_BASE     (default: https://api.digikey.com)
    DIGIKEY_LOCALE_SITE  (default: ES)
    DIGIKEY_CURRENCY     (default: EUR)
    DIGIKEY_LOCALE_LANG  (default: en)
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import httpx

from kicad_claude.utils.kicad_paths import cache_dir

logger = logging.getLogger("kicad-claude.adapters.digikey")

DEFAULT_BASE = "https://api.digikey.com"
TOKEN_LEEWAY_SEC = 60  # refresh a minute before actual expiry


class DigiKeyError(RuntimeError):
    """API call failed (network, auth, or 4xx/5xx)."""


def _credentials() -> tuple[str, str]:
    cid = os.environ.get("DIGIKEY_CLIENT_ID")
    sec = os.environ.get("DIGIKEY_CLIENT_SECRET")
    if not cid or not sec:
        raise DigiKeyError(
            "DIGIKEY_CLIENT_ID / DIGIKEY_CLIENT_SECRET not set; "
            "configure them in .env"
        )
    return cid, sec


def _base_url() -> str:
    return os.environ.get("DIGIKEY_API_BASE", DEFAULT_BASE).rstrip("/")


def _token_cache_path():
    return cache_dir() / "digikey_token.json"


def _load_cached_token() -> str | None:
    p = _token_cache_path()
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("expires_at", 0) < time.time() + TOKEN_LEEWAY_SEC:
        return None
    if data.get("client_id") != _credentials()[0]:
        # Different client — invalidate.
        return None
    return data.get("access_token")


def _save_cached_token(token: str, expires_in: int) -> None:
    cid, _ = _credentials()
    payload = {
        "access_token": token,
        "expires_at": int(time.time()) + int(expires_in),
        "client_id": cid,
    }
    _token_cache_path().write_text(json.dumps(payload))


def _fetch_token() -> str:
    cid, sec = _credentials()
    url = f"{_base_url()}/v1/oauth2/token"
    logger.info("requesting DigiKey OAuth token")
    try:
        r = httpx.post(
            url,
            data={
                "client_id": cid,
                "client_secret": sec,
                "grant_type": "client_credentials",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=20.0,
        )
    except httpx.HTTPError as e:
        raise DigiKeyError(f"network error fetching token: {e}") from e
    if r.status_code != 200:
        raise DigiKeyError(
            f"OAuth token request failed: {r.status_code} {r.text[:200]}"
        )
    payload = r.json()
    token = payload["access_token"]
    _save_cached_token(token, payload.get("expires_in", 600))
    return token


def get_access_token() -> str:
    cached = _load_cached_token()
    if cached:
        return cached
    return _fetch_token()


def _request(method: str, path: str, *, json_body: Any = None, params: dict | None = None) -> Any:
    """Authenticated request to DigiKey V4. Refreshes token on 401 once."""
    cid, _ = _credentials()
    headers = {
        "Authorization": f"Bearer {get_access_token()}",
        "X-DIGIKEY-Client-Id": cid,
        "X-DIGIKEY-Locale-Site": os.environ.get("DIGIKEY_LOCALE_SITE", "ES"),
        "X-DIGIKEY-Locale-Currency": os.environ.get("DIGIKEY_CURRENCY", "EUR"),
        "X-DIGIKEY-Locale-Language": os.environ.get("DIGIKEY_LOCALE_LANG", "en"),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    url = f"{_base_url()}{path}"
    try:
        r = httpx.request(
            method, url, json=json_body, params=params, headers=headers, timeout=20.0
        )
    except httpx.HTTPError as e:
        raise DigiKeyError(f"network error: {e}") from e

    if r.status_code == 401:
        # Token might have been invalidated server-side; one retry.
        logger.info("DigiKey 401 — refreshing token and retrying")
        _token_cache_path().unlink(missing_ok=True)
        headers["Authorization"] = f"Bearer {_fetch_token()}"
        r = httpx.request(
            method, url, json=json_body, params=params, headers=headers, timeout=20.0
        )

    if r.status_code >= 400:
        raise DigiKeyError(f"{method} {path} → {r.status_code}: {r.text[:300]}")
    return r.json()


# --------------------------------------------------------------------------- #
# Public methods
# --------------------------------------------------------------------------- #


def search_keyword(query: str, limit: int = 5) -> list[dict]:
    """Keyword search. Returns simplified part summaries."""
    payload = _request(
        "POST",
        "/products/v4/search/keyword",
        json_body={"Keywords": query, "Limit": limit, "Offset": 0},
    )
    return _summarize_search(payload)


def get_product_details(part_number: str) -> dict | None:
    """Look up a specific MPN. Returns first matching product or None."""
    safe = httpx.URL(part_number).path.replace("/", "%2F")
    try:
        payload = _request("GET", f"/products/v4/search/{safe}/productdetails")
    except DigiKeyError as e:
        if "404" in str(e):
            return None
        raise
    product = payload.get("Product")
    if not product:
        return None
    return _summarize_product(product)


# --------------------------------------------------------------------------- #
# Response shaping
# --------------------------------------------------------------------------- #


def _summarize_search(payload: dict) -> list[dict]:
    products = payload.get("Products") or []
    return [_summarize_product(p) for p in products]


def _summarize_product(p: dict) -> dict:
    mfr = (p.get("Manufacturer") or {}).get("Name", "")
    mpn = p.get("ManufacturerProductNumber", "")
    desc = p.get("Description", {}) or {}
    description = desc.get("ProductDescription") or desc.get("DetailedDescription") or ""

    stock = p.get("QuantityAvailable", 0)
    unit_price = p.get("UnitPrice", 0.0)

    # ECAD model URLs (for SnapEDA / Ultra Librarian) live here when available.
    classifications = p.get("Classifications") or {}
    media = p.get("MediaLinks") or []

    return {
        "source": "digikey",
        "mpn": mpn,
        "manufacturer": mfr,
        "description": description,
        "stock": stock,
        "unit_price": unit_price,
        "currency": os.environ.get("DIGIKEY_CURRENCY", "EUR"),
        "datasheet_url": p.get("DatasheetUrl") or "",
        "product_url": p.get("ProductUrl") or "",
        "category": (classifications.get("ParentCategory") or {}).get("Name", ""),
        "ecad_models": [
            m.get("Url") for m in media if "ECAD" in (m.get("Title") or "").upper()
        ],
    }
