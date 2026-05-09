"""Fuzzy search across the indexed library entries.

Uses `rapidfuzz` for ranking. The "haystack" for each entry is a single
string built from `lib_id + description + keywords/tags` so a query like
"esp32-s3" matches both library-id substrings and keyword text.
"""

from __future__ import annotations

from typing import Any

from rapidfuzz import fuzz, process, utils


def _symbol_haystack(entry: dict[str, Any]) -> str:
    return " ".join(
        filter(
            None,
            (
                entry.get("lib_id", ""),
                entry.get("description", ""),
                entry.get("keywords", ""),
            ),
        )
    )


def _footprint_haystack(entry: dict[str, Any]) -> str:
    return " ".join(
        filter(
            None,
            (
                entry.get("lib_id", ""),
                entry.get("description", ""),
                entry.get("tags", ""),
            ),
        )
    )


def _search(
    query: str,
    entries: dict[str, dict[str, Any]],
    haystack_fn,
    max_results: int,
    score_cutoff: int,
) -> list[dict[str, Any]]:
    if not query.strip() or not entries:
        return []

    # Build (lib_id, haystack) parallel lists once.
    lib_ids = list(entries.keys())
    haystacks = [haystack_fn(entries[k]) for k in lib_ids]

    # default_process lowercases and strips non-alphanumeric, so "ESP32-S3"
    # and "esp32 s3" normalize to the same form before scoring.
    results = process.extract(
        query,
        haystacks,
        scorer=fuzz.WRatio,
        processor=utils.default_process,
        limit=max_results,
        score_cutoff=score_cutoff,
    )
    out = []
    for _haystack, score, idx in results:
        entry = dict(entries[lib_ids[idx]])
        entry["_score"] = round(score, 1)
        out.append(entry)
    return out


def search_symbols(
    query: str,
    index: dict[str, Any],
    max_results: int = 10,
    score_cutoff: int = 50,
) -> list[dict[str, Any]]:
    return _search(
        query,
        index.get("symbols", {}),
        _symbol_haystack,
        max_results,
        score_cutoff,
    )


def search_footprints(
    query: str,
    index: dict[str, Any],
    max_results: int = 10,
    score_cutoff: int = 50,
) -> list[dict[str, Any]]:
    return _search(
        query,
        index.get("footprints", {}),
        _footprint_haystack,
        max_results,
        score_cutoff,
    )
