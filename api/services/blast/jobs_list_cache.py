"""Bounded, thread-safe TTL cache for ``/api/blast/jobs`` list responses.

Responsibility: In-process LRU+TTL cache for the BLAST jobs-list response payload
Edit boundaries: Pure in-memory cache infrastructure — no HTTP, auth, Azure, or row-shaping
logic here. The route module owns request handling and calls these helpers.
Key entry points: `jobs_list_cache_key`, `jobs_list_cache_get`, `jobs_list_cache_set`,
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

# The frontend polls ``/api/blast/jobs`` every ~14 s. A 10 s TTL keeps the
# common case (single user staring at the Jobs page) as a cache hit while
# tab-switching or page reloads still see fresh data within one cycle.
JOBS_LIST_CACHE_TTL_SECONDS = 10.0
JOBS_LIST_CACHE_MAX_ENTRIES = 128

# Store the serialized JSON bytes so cache get/set never deepcopies. JSON
# round-trip gives callers a fresh mutable dict (same isolation as a deep
# copy) without ``copy.deepcopy``'s O(N) traversal of nested lists. The
# OrderedDict supports O(1) LRU eviction via ``popitem(last=False)``.
_JOBS_LIST_CACHE: OrderedDict[str, tuple[float, bytes]] = OrderedDict()
_JOBS_LIST_CACHE_LOCK = threading.Lock()


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
    now = time.monotonic()
    with _JOBS_LIST_CACHE_LOCK:
        entry = _JOBS_LIST_CACHE.get(key)
        if entry is None:
            return None
        expires_at, payload_bytes = entry
        if expires_at <= now:
            _JOBS_LIST_CACHE.pop(key, None)
            return None
        # Touch for LRU semantics so frequently-read entries stay warm.
        _JOBS_LIST_CACHE.move_to_end(key)
    # json.loads outside the lock — deserialization is the only per-call
    # cost and we don't want it blocking other readers.
    decoded = json.loads(payload_bytes)
    return decoded if isinstance(decoded, dict) else None


def jobs_list_cache_set(key: str, response: dict[str, Any]) -> None:
    payload_bytes = json.dumps(response, separators=(",", ":")).encode("utf-8")
    expires_at = time.monotonic() + JOBS_LIST_CACHE_TTL_SECONDS
    with _JOBS_LIST_CACHE_LOCK:
        # Replacing an existing key needs explicit pop so move_to_end-on-set
        # semantics don't collide with the LRU bookkeeping.
        _JOBS_LIST_CACHE.pop(key, None)
        _JOBS_LIST_CACHE[key] = (expires_at, payload_bytes)
        while len(_JOBS_LIST_CACHE) > JOBS_LIST_CACHE_MAX_ENTRIES:
            _JOBS_LIST_CACHE.popitem(last=False)


def reset_jobs_list_cache() -> None:
    with _JOBS_LIST_CACHE_LOCK:
        _JOBS_LIST_CACHE.clear()
