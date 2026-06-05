"""Process-local TTL cache for the BLAST database catalogue listing.

Responsibility: Cache the expensive ``blast-db`` container enumeration (full
blob list + per-DB metadata reads) for the read path (``GET /api/blast/databases``)
so a workspace with many databases opens New Search without re-paying N Storage
round-trips on every page load. Correctness is event-driven: admin actions that
mutate a database (prepare / delete / shard) publish on the shared
``db_metadata`` Redis channel, whose subscriber drops this cache too; a short TTL
backstop bounds staleness from out-of-band changes (terminal ``azcopy``, NCBI
auto-refresh) that never reach our code.
Edit boundaries: Pure in-process caching + single-flight only. The underlying
enumeration lives in ``database_list.list_databases`` (reached via the
``storage.data`` facade so tests can monkeypatch it); cross-sidecar invalidation
wiring lives in ``blast.db_metadata``.
Key entry points: ``list_databases_cached``, ``invalidate_blast_db_listing_cache``,
``_reset_blast_db_listing_cache``.
Risky contracts: Cached payloads are stored as JSON bytes and a fresh mutable
list is returned on every hit, so the route's ``warmup_plan`` enrichment never
mutates the shared cache entry. The cache key is the Storage account only — the
listing is account-wide and topology-independent, so a cluster switch reuses it.
Validation: ``uv run pytest -q api/tests/test_database_catalog_cache.py``.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import OrderedDict
from typing import Any

from azure.core.credentials import TokenCredential

LOGGER = logging.getLogger(__name__)

# The catalogue changes only when an admin prepares / deletes / shards a DB,
# and every such write publishes an invalidation on the shared Redis channel
# (see ``blast.db_metadata``). With explicit invalidation the TTL exists only
# as a backstop for out-of-band changes our code never observes, so it can be
# generous without serving stale data after an in-app action.
_CACHE_TTL_SECONDS = float(os.environ.get("BLAST_DB_CATALOG_CACHE_TTL", "300.0"))
_CACHE_MAX_ENTRIES = 64

# (account, container) -> (expires_at_monotonic, payload_bytes)
_CACHE: OrderedDict[tuple[str, str], tuple[float, bytes]] = OrderedDict()
_CACHE_LOCK = threading.Lock()

# Per-account generation counter. ``invalidate_blast_db_listing_cache`` bumps it
# so a single-flight leader that started its enumeration BEFORE an invalidation
# (an admin deleting / preparing a DB mid-enumeration) can detect that its result
# is already stale and decline to cache it. Without this guard the leader would
# overwrite the just-invalidated entry with a pre-change snapshot and pin it for
# the whole TTL — the classic "invalidate races a cold fill" defect. The caller
# still receives the leader's result (best available), it is just not cached.
_EPOCH: dict[str, int] = {}

# Single-flight coordination mirrors ``blast.db_metadata`` so concurrent New
# Search loads on a cold key only pay the enumeration once. A leader that dies
# before clearing its entry cannot wedge fresh readers forever: the TTL guard
# below re-elects after ``_INFLIGHT_TTL_SECONDS``.
_INFLIGHT: dict[tuple[str, str], tuple[threading.Event, float]] = {}
_INFLIGHT_TTL_SECONDS = 60.0
# How long a follower waits for the leader before electing itself. Slightly
# above the worst-case enumeration wall time for a large multi-volume DB list.
_INFLIGHT_WAIT_SECONDS = 20.0


def _delegate_list(
    storage_data: Any,
    credential: TokenCredential,
    account_name: str,
    container: str,
) -> list[dict[str, Any]]:
    """Call the ``storage.data`` facade enumeration, matching its call contract.

    The historical callers invoke ``list_databases(cred, account)`` with two
    positional args for the default ``blast-db`` container, and the test fakes
    are written to that 2-arg shape. Preserve it so this cache wrapper is a
    drop-in replacement; only pass the third arg for a non-default container.
    """
    if container == "blast-db":
        return storage_data.list_databases(credential, account_name)
    return storage_data.list_databases(credential, account_name, container)


def list_databases_cached(
    credential: TokenCredential,
    account_name: str,
    container: str = "blast-db",
    *,
    force_refresh: bool = False,
) -> list[dict[str, Any]]:
    """Return the cached BLAST database catalogue for ``account_name``.

    On a cache miss this delegates to ``storage.data.list_databases`` (the
    facade tests monkeypatch), serialises the result to JSON bytes, and stores
    it under a per-account key. Every hit returns a freshly deserialised list
    so callers can mutate it (e.g. attach ``warmup_plan``) without corrupting
    the shared entry.

    ``force_refresh=True`` bypasses any cached entry, re-enumerates Storage, and
    refreshes the cache with the result. This backs the explicit "Refresh"
    affordance on the Database Builder so that an out-of-band change (a terminal
    ``azcopy`` upload that never published an invalidation) is reflected
    immediately instead of waiting out the TTL backstop.

    The empty/degraded case (no account) is never cached — it delegates
    straight through so a transient Storage failure is not pinned for the TTL.
    Enumeration failures propagate to the caller (the route classifies them
    into a degraded payload) and are likewise not cached.
    """
    from api.services.storage import data as _storage_data

    if not account_name:
        return _delegate_list(_storage_data, credential, account_name, container)

    cache_key = (account_name, container)

    if force_refresh:
        # Bypass the cache, re-enumerate, and refresh the entry. Capture the
        # epoch up front so a concurrent invalidation during this fetch still
        # prevents us pinning a pre-change snapshot (same guard as the
        # single-flight path below).
        with _CACHE_LOCK:
            epoch_at_start = _EPOCH.get(account_name, 0)
        result = _delegate_list(_storage_data, credential, account_name, container)
        payload = json.dumps(result, separators=(",", ":")).encode("utf-8")
        expires_at = time.monotonic() + _CACHE_TTL_SECONDS
        with _CACHE_LOCK:
            if _EPOCH.get(account_name, 0) == epoch_at_start:
                _CACHE.pop(cache_key, None)
                _CACHE[cache_key] = (expires_at, payload)
                while len(_CACHE) > _CACHE_MAX_ENTRIES:
                    _CACHE.popitem(last=False)
        return json.loads(payload)

    while True:
        now = time.monotonic()
        with _CACHE_LOCK:
            cached = _CACHE.get(cache_key)
            if cached and cached[0] > now:
                _CACHE.move_to_end(cache_key)
                return json.loads(cached[1])
            inflight_entry = _INFLIGHT.get(cache_key)
            if inflight_entry is not None:
                inflight, registered_at = inflight_entry
                if now - registered_at > _INFLIGHT_TTL_SECONDS:
                    # Leader took too long (crash / SIGTERM race) — wake any
                    # sleepers and re-elect rather than block forever.
                    inflight.set()
                    _INFLIGHT.pop(cache_key, None)
                    inflight_entry = None
            if inflight_entry is None:
                inflight = threading.Event()
                _INFLIGHT[cache_key] = (inflight, now)
                # Snapshot the account epoch while holding the lock so we can
                # detect an invalidation that lands while we enumerate.
                epoch_at_start = _EPOCH.get(account_name, 0)
                leader = True
            else:
                inflight = inflight_entry[0]
                leader = False
        if not leader:
            inflight.wait(timeout=_INFLIGHT_WAIT_SECONDS)
            continue
        try:
            result = _delegate_list(_storage_data, credential, account_name, container)
            payload = json.dumps(result, separators=(",", ":")).encode("utf-8")
            expires_at = time.monotonic() + _CACHE_TTL_SECONDS
            with _CACHE_LOCK:
                # Only commit if no invalidation happened during the fill. If
                # the epoch advanced, the data we just read may predate the
                # change that triggered the invalidation, so we return it to the
                # caller but do NOT cache it — the next read re-enumerates.
                if _EPOCH.get(account_name, 0) == epoch_at_start:
                    _CACHE.pop(cache_key, None)
                    _CACHE[cache_key] = (expires_at, payload)
                    while len(_CACHE) > _CACHE_MAX_ENTRIES:
                        _CACHE.popitem(last=False)
            return json.loads(payload)
        finally:
            with _CACHE_LOCK:
                _INFLIGHT.pop(cache_key, None)
                inflight.set()


def invalidate_blast_db_listing_cache(account_name: str | None = None) -> int:
    """Drop cached catalogue listings for one account or all accounts.

    - ``account_name`` set: remove every container entry for that account.
    - ``None``: clear the cache entirely (equivalent to the test reset).

    Returns the number of entries removed. Pure in-process state, no I/O, safe
    to call from any sidecar. Cross-sidecar fan-out is handled by the shared
    ``db_metadata`` Redis pub/sub channel, whose subscriber calls this.
    """
    account_key = (account_name or "").strip()
    with _CACHE_LOCK:
        if not account_key:
            removed = len(_CACHE)
            _CACHE.clear()
            _INFLIGHT.clear()
            # Bump every known account's epoch so any in-flight leader on any
            # account declines to cache its now-stale fill.
            for key in list(_EPOCH):
                _EPOCH[key] += 1
            return removed
        # Bump this account's epoch first so a concurrent leader mid-fill sees
        # the change and refuses to cache its pre-change snapshot.
        _EPOCH[account_key] = _EPOCH.get(account_key, 0) + 1
        to_drop = [key for key in _CACHE if key[0] == account_key]
        for key in to_drop:
            _CACHE.pop(key, None)
        return len(to_drop)


def _reset_blast_db_listing_cache() -> None:
    """Test hook: drop the cached catalogue listing (and inflight + epoch state)."""

    invalidate_blast_db_listing_cache()
    with _CACHE_LOCK:
        _EPOCH.clear()
