"""In-memory TTL cache for ARM resource discovery results (storage/ACR lists).

Responsibility: Bounded, thread-safe TTL cache keyed by (kind, subscription, resource group)
for ARM discovery payloads consumed by the `/api/arm/*` routes.
Edit boundaries: Pure in-memory bookkeeping only — no Azure SDK, no HTTP, no FastAPI imports.
The discovery routes own the actual ARM calls and response shaping.
Key entry points: `cached_discovery`, `store_discovery`.
Risky contracts: Every read-modify-write must hold `_LOCK` — the `/api/arm/*` routes are
synchronous `def` handlers that FastAPI runs in its worker threadpool, so concurrent eviction
(`min(...)` iterating the dict while another thread inserts) would otherwise raise
`RuntimeError: dictionary changed size during iteration` and surface as a 500. Returned lists
are deep-copied so callers cannot mutate cached entries.
Validation: `uv run pytest -q api/tests/test_arm_discovery_cache.py`.
"""

from __future__ import annotations

import threading
import time
from typing import Any

DISCOVERY_CACHE_TTL_SECONDS = 60.0
DISCOVERY_CACHE_MAX_ENTRIES = 512

_CACHE: dict[tuple[str, str, str], tuple[float, list[dict[str, Any]]]] = {}
# Guards every read-modify-write on ``_CACHE``. See the module Risky contracts note:
# the discovery routes run in FastAPI's worker threadpool, so the eviction ``min(...)``
# below can iterate the dict while another thread inserts a new key. The critical
# section is pure in-memory bookkeeping (no Azure I/O), so holding the lock is cheap.
_LOCK = threading.Lock()


def cached_discovery(kind: str, subscription_id: str, rg: str) -> list[dict[str, Any]] | None:
    """Return a deep copy of the cached discovery list, or ``None`` on miss/expiry."""
    key = (kind, subscription_id, rg)
    with _LOCK:
        cached = _CACHE.get(key)
        if cached is None:
            return None
        expires_at, value = cached
        if expires_at <= time.monotonic():
            _CACHE.pop(key, None)
            return None
        return [dict(item) for item in value]


def store_discovery(
    kind: str, subscription_id: str, rg: str, value: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Cache ``value`` under (kind, subscription, rg) and return it unchanged."""
    key = (kind, subscription_id, rg)
    with _LOCK:
        if key not in _CACHE and len(_CACHE) >= DISCOVERY_CACHE_MAX_ENTRIES:
            # TTL eviction only fires on read, so an entry written once and never
            # read again would linger forever. Bound the cache by dropping the
            # entry closest to expiry whenever a new key would overflow it.
            soonest = min(_CACHE, key=lambda k: _CACHE[k][0])
            _CACHE.pop(soonest, None)
        _CACHE[key] = (
            time.monotonic() + DISCOVERY_CACHE_TTL_SECONDS,
            [dict(item) for item in value],
        )
    return value
