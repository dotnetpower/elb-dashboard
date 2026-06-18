"""Bounded, thread-safe TTL cache for ``/api/blast/jobs`` list responses.

Responsibility: In-process LRU+TTL cache for the BLAST jobs-list response payload
Edit boundaries: Pure in-memory cache infrastructure — no HTTP, auth, Azure, or row-shaping
logic here. The route module owns request handling and calls these helpers.
Key entry points: `jobs_list_cache_key`, `jobs_list_cache_get`, `jobs_list_cache_get_swr`,
`jobs_list_cache_set`, `begin_jobs_list_revalidate`, `end_jobs_list_revalidate`,
`reset_jobs_list_cache`
Risky contracts: Cache get/set must hold `_JOBS_LIST_CACHE_LOCK` for every read-modify-write
(including LRU eviction) so concurrent threadpool requests cannot mutate the OrderedDict during
iteration. JSON (de)serialization stays OUTSIDE the lock so it never blocks other readers, and
gives callers an isolated mutable dict without `copy.deepcopy`.
Validation: `uv run pytest -q api/tests/test_jobs_list_cache.py
api/tests/test_blast_results_routes.py`.
"""

from __future__ import annotations

import json
import threading
import time
from collections import OrderedDict
from typing import Any

# The frontend polls ``/api/blast/jobs`` every ~14 s. A 10 s fresh TTL keeps the
# common case (single user staring at the Jobs page) as a cache hit while
# tab-switching or page reloads still see fresh data within one cycle.
#
# Beyond the fresh window the entry is served **stale** for up to
# ``JOBS_LIST_CACHE_STALE_TTL_SECONDS`` while a single background revalidation
# rebuilds it (stale-while-revalidate). This hides the cold-build latency — the
# subscription-wide listing fans out to external OpenAPI discovery + per-cluster
# ``/v1/jobs`` fetches plus per-job K8s status refresh that, on a cold key, were
# measured at p90 ~250 s / max ~20 min against a busy fleet — from the polling
# caller.
#
# The stale ceiling MUST comfortably exceed a worst-case cold rebuild. When it
# did not (the original 70 s, aligned only with the external jobs cache TTL), a
# rebuild slower than 70 s let the entry expire to *cold* before the background
# task finished, so the next poll fell down the synchronous build path and
# blocked for minutes — the dashboard "JOBS loading…" spinner that never
# resolved. 600 s covers the observed p90 and keeps idle dashboards responsive;
# effective staleness is still ~one rebuild cadence because every stale poll
# triggers a rebuild that resets the window with fresh data. As a belt-and-
# braces guard, ``jobs_list_cache_get_swr`` additionally refuses to drop an
# entry while a revalidation is actively in flight (see below), so a rebuild
# that overruns the ceiling still never sends a caller down the cold path.
JOBS_LIST_CACHE_TTL_SECONDS = 10.0
JOBS_LIST_CACHE_STALE_TTL_SECONDS = 600.0
JOBS_LIST_CACHE_MAX_ENTRIES = 128

# Store the serialized JSON bytes so cache get/set never deepcopies. JSON
# round-trip gives callers a fresh mutable dict (same isolation as a deep
# copy) without ``copy.deepcopy``'s O(N) traversal of nested lists. The
# OrderedDict supports O(1) LRU eviction via ``popitem(last=False)``.
#
# Entry shape: ``(fresh_until, hard_until, payload_bytes)`` — ``fresh_until``
# bounds the fresh window, ``hard_until`` the stale window; past ``hard_until``
# the entry is dropped on read.
_JOBS_LIST_CACHE: OrderedDict[str, tuple[float, float, bytes]] = OrderedDict()
_JOBS_LIST_CACHE_LOCK = threading.Lock()

# Single-flight guard for stale-while-revalidate: only one background rebuild
# per cache key runs at a time, so a burst of polling requests that all see the
# same stale entry enqueues exactly one revalidation instead of N. A rebuild
# that never calls ``end_jobs_list_revalidate`` (crash) cannot wedge future
# revalidations forever — the next ``begin_jobs_list_revalidate`` past the TTL
# re-elects.
#
# This TTL is intentionally generous because it doubles as the crash-safety
# upper bound on the "entry retention" window in ``jobs_list_cache_get_swr`` (an
# in-flight rebuild keeps the stale entry alive past the hard ceiling). Normal
# completion releases the slot immediately via ``end_jobs_list_revalidate`` in
# the rebuild's ``finally``, so this TTL only bounds a *truly hung* rebuild
# (Azure SDK calls carry their own timeouts, so that should not happen). It must
# exceed a worst-case cold rebuild (observed p90 ~250 s, max ~20 min) so that an
# entry claimed for revalidation at any point in its stale window stays retained
# until the rebuild lands, instead of dropping to cold and blocking the poll.
_JOBS_LIST_REVALIDATE_INFLIGHT: dict[str, float] = {}
_JOBS_LIST_REVALIDATE_TTL_SECONDS = 1800.0


def jobs_list_cache_key(
    *,
    caller_oid: str,
    limit: int,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    shared_visibility: bool,
) -> str:
    return json.dumps(
        {
            "caller_oid": caller_oid,
            "limit": limit,
            "subscription_id": subscription_id,
            "resource_group": resource_group,
            "cluster_name": cluster_name,
            "shared_visibility": shared_visibility,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def jobs_list_cache_get(key: str) -> dict[str, Any] | None:
    """Return the payload only while it is FRESH (legacy strict-freshness read).

    Kept for callers/tests that want the original "fresh-or-nothing" contract.
    The route uses :func:`jobs_list_cache_get_swr` for stale-while-revalidate.
    """
    payload, is_stale = jobs_list_cache_get_swr(key)
    if payload is None or is_stale:
        return None
    return payload


def jobs_list_cache_get_swr(key: str) -> tuple[dict[str, Any] | None, bool]:
    """Stale-while-revalidate read.

    Returns ``(payload, is_stale)``:
    - fresh entry  → ``(payload, False)``
    - stale entry  → ``(payload, True)``  (caller should trigger a background rebuild)
    - cold / past the stale ceiling → ``(None, False)``

    Retention exception: an entry past the stale ceiling is still returned as
    ``(payload, True)`` — not dropped — while a background revalidation is
    actively in flight for the key. This guarantees a poll never falls down the
    cold synchronous build path while a rebuild it (or a peer) already triggered
    is still running, so a rebuild slower than the ceiling cannot resurrect the
    "JOBS loading…" spinner.
    """
    now = time.monotonic()
    with _JOBS_LIST_CACHE_LOCK:
        entry = _JOBS_LIST_CACHE.get(key)
        if entry is None:
            return None, False
        fresh_until, hard_until, payload_bytes = entry
        if hard_until <= now:
            # Past the stale ceiling. Normally drop the entry so the next caller
            # pays a fresh synchronous build. BUT if a background revalidation
            # is already rebuilding this key, dropping it would push that caller
            # down the cold synchronous path — a multi-second/minute build that
            # blocks the poll — even though a rebuild is already in flight.
            # Retain the (very) stale payload and keep serving it until the
            # in-flight rebuild lands a fresh entry, so a rebuild that overruns
            # the ceiling never causes the "JOBS loading…" spinner to reappear.
            started_at = _JOBS_LIST_REVALIDATE_INFLIGHT.get(key)
            revalidating = (
                started_at is not None
                and now - started_at < _JOBS_LIST_REVALIDATE_TTL_SECONDS
            )
            if not revalidating:
                _JOBS_LIST_CACHE.pop(key, None)
                return None, False
            _JOBS_LIST_CACHE.move_to_end(key)
            is_stale = True
        else:
            # Touch for LRU semantics so frequently-read entries stay warm.
            _JOBS_LIST_CACHE.move_to_end(key)
            is_stale = fresh_until <= now
    # json.loads outside the lock — deserialization is the only per-call
    # cost and we don't want it blocking other readers.
    decoded = json.loads(payload_bytes)
    if not isinstance(decoded, dict):
        return None, False
    return decoded, is_stale


def jobs_list_cache_set(key: str, response: dict[str, Any]) -> None:
    payload_bytes = json.dumps(response, separators=(",", ":")).encode("utf-8")
    now = time.monotonic()
    fresh_until = now + JOBS_LIST_CACHE_TTL_SECONDS
    hard_until = now + JOBS_LIST_CACHE_STALE_TTL_SECONDS
    with _JOBS_LIST_CACHE_LOCK:
        # Replacing an existing key needs explicit pop so move_to_end-on-set
        # semantics don't collide with the LRU bookkeeping.
        _JOBS_LIST_CACHE.pop(key, None)
        _JOBS_LIST_CACHE[key] = (fresh_until, hard_until, payload_bytes)
        while len(_JOBS_LIST_CACHE) > JOBS_LIST_CACHE_MAX_ENTRIES:
            _JOBS_LIST_CACHE.popitem(last=False)


def begin_jobs_list_revalidate(key: str) -> bool:
    """Try to claim the single-flight slot for a background rebuild of ``key``.

    Returns True if the caller is now the leader (and MUST call
    :func:`end_jobs_list_revalidate` when done). Returns False if another
    rebuild is already in flight (within the TTL) — the caller should just
    serve the stale entry without enqueuing a duplicate rebuild.
    """
    now = time.monotonic()
    with _JOBS_LIST_CACHE_LOCK:
        started_at = _JOBS_LIST_REVALIDATE_INFLIGHT.get(key)
        if started_at is not None and now - started_at < _JOBS_LIST_REVALIDATE_TTL_SECONDS:
            return False
        _JOBS_LIST_REVALIDATE_INFLIGHT[key] = now
        return True


def end_jobs_list_revalidate(key: str) -> None:
    with _JOBS_LIST_CACHE_LOCK:
        _JOBS_LIST_REVALIDATE_INFLIGHT.pop(key, None)


def reset_jobs_list_cache() -> None:
    with _JOBS_LIST_CACHE_LOCK:
        _JOBS_LIST_CACHE.clear()
        _JOBS_LIST_REVALIDATE_INFLIGHT.clear()
