"""In-memory TTL caches for taxonomy search/detail/siblings results."""

from __future__ import annotations

import threading
import time
from typing import Any

DEFAULT_CACHE_TTL_SECONDS = 24 * 60 * 60
MAX_DETAIL_CACHE_ENTRIES = 1024

_CACHE_LOCK = threading.Lock()
_CACHE: dict[tuple[str, int], tuple[float, dict[str, Any]]] = {}

_DETAIL_CACHE_LOCK = threading.Lock()
_DETAIL_CACHE: dict[int, tuple[float, dict[str, Any]]] = {}

_SIBLINGS_CACHE_LOCK = threading.Lock()
_SIBLINGS_CACHE: dict[tuple[int, str], tuple[float, dict[str, Any]]] = {}


def clear_taxonomy_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


def _cache_get(cache_key: tuple[str, int]) -> dict[str, Any] | None:
    now = time.monotonic()
    with _CACHE_LOCK:
        item = _CACHE.get(cache_key)
        if item is None:
            return None
        expires_at, payload = item
        if expires_at <= now:
            _CACHE.pop(cache_key, None)
            return None
        return dict(payload)


def _cache_put(cache_key: tuple[str, int], payload: dict[str, Any]) -> None:
    with _CACHE_LOCK:
        _CACHE[cache_key] = (time.monotonic() + DEFAULT_CACHE_TTL_SECONDS, dict(payload))


def _detail_cache_get(taxid: int) -> dict[str, Any] | None:
    with _DETAIL_CACHE_LOCK:
        entry = _DETAIL_CACHE.get(taxid)
        if entry is None:
            return None
        expires_at, payload = entry
        if expires_at < time.monotonic():
            _DETAIL_CACHE.pop(taxid, None)
            return None
        return dict(payload)


def _detail_cache_put(taxid: int, payload: dict[str, Any]) -> None:
    with _DETAIL_CACHE_LOCK:
        if len(_DETAIL_CACHE) >= MAX_DETAIL_CACHE_ENTRIES:
            # Drop the oldest entry (insertion order). Cheap LRU-ish eviction
            # without an extra dependency.
            try:
                oldest_key = next(iter(_DETAIL_CACHE))
                _DETAIL_CACHE.pop(oldest_key, None)
            except StopIteration:
                pass
        _DETAIL_CACHE[taxid] = (
            time.monotonic() + DEFAULT_CACHE_TTL_SECONDS,
            dict(payload),
        )


def clear_taxonomy_detail_cache() -> None:
    with _DETAIL_CACHE_LOCK:
        _DETAIL_CACHE.clear()


# ---------------------------------------------------------------------------
# Siblings / tree — fetch direct taxonomic siblings at each major rank
# for the cladogram visualisation.  Caches per (parent_taxid, rank) pair.
# ---------------------------------------------------------------------------

_MAJOR_RANKS_SET = frozenset(
    [
        "superkingdom",
        "kingdom",
        "phylum",
        "class",
        "order",
        "family",
        "genus",
        "species",
    ]
)

_SIBLINGS_CACHE_LOCK = threading.Lock()
_SIBLINGS_CACHE: dict[tuple[int, str, int], tuple[float, list[dict[str, Any]]]] = {}
MAX_SIBLINGS_CACHE_ENTRIES = 512


def _siblings_cache_get(
    key: tuple[int, str, int],
) -> list[dict[str, Any]] | None:
    with _SIBLINGS_CACHE_LOCK:
        entry = _SIBLINGS_CACHE.get(key)
        if entry is None:
            return None
        expires_at, payload = entry
        if expires_at < time.monotonic():
            _SIBLINGS_CACHE.pop(key, None)
            return None
        return list(payload)


def _siblings_cache_put(
    key: tuple[int, str, int],
    payload: list[dict[str, Any]],
) -> None:
    with _SIBLINGS_CACHE_LOCK:
        if len(_SIBLINGS_CACHE) >= MAX_SIBLINGS_CACHE_ENTRIES:
            try:
                oldest = next(iter(_SIBLINGS_CACHE))
                _SIBLINGS_CACHE.pop(oldest, None)
            except StopIteration:
                pass
        _SIBLINGS_CACHE[key] = (
            time.monotonic() + DEFAULT_CACHE_TTL_SECONDS,
            list(payload),
        )


def clear_taxonomy_siblings_cache() -> None:
    with _SIBLINGS_CACHE_LOCK:
        _SIBLINGS_CACHE.clear()
