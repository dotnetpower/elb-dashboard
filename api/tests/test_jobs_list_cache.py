"""Unit tests for the BLAST jobs-list response cache.

Responsibility: Verify the in-process LRU+TTL cache backing ``/api/blast/jobs``
Edit boundaries: Test-only; exercises `api.services.blast.jobs_list_cache` in isolation.
Key entry points: `test_miss_then_hit`, `test_get_returns_isolated_dict`,
`test_ttl_expiry`, `test_lru_eviction_bound`, `test_concurrent_set_get_no_crash`
Risky contracts: The cache must never raise under concurrent threadpool access and must keep
the entry count at or below `JOBS_LIST_CACHE_MAX_ENTRIES`.
Validation: `uv run pytest -q api/tests/test_jobs_list_cache.py`.
"""

from __future__ import annotations

import threading

import api.services.blast.jobs_list_cache as cache_mod
from api.services.blast.jobs_list_cache import (
    JOBS_LIST_CACHE_MAX_ENTRIES,
    jobs_list_cache_get,
    jobs_list_cache_key,
    jobs_list_cache_set,
    reset_jobs_list_cache,
)


def _key(oid: str) -> str:
    return jobs_list_cache_key(
        caller_oid=oid,
        limit=50,
        subscription_id="sub",
        resource_group="rg",
        cluster_name="cl",
        shared_visibility=False,
    )


def setup_function() -> None:
    reset_jobs_list_cache()


def test_miss_then_hit() -> None:
    key = _key("a")
    assert jobs_list_cache_get(key) is None
    jobs_list_cache_set(key, {"jobs": [1, 2, 3]})
    assert jobs_list_cache_get(key) == {"jobs": [1, 2, 3]}


def test_get_returns_isolated_dict() -> None:
    key = _key("b")
    jobs_list_cache_set(key, {"jobs": [{"id": 1}]})
    first = jobs_list_cache_get(key)
    assert first is not None
    first["jobs"].append({"id": 2})
    # Mutating the returned dict must not corrupt the cached payload.
    second = jobs_list_cache_get(key)
    assert second == {"jobs": [{"id": 1}]}


def test_ttl_expiry(monkeypatch) -> None:
    clock = {"now": 1000.0}
    monkeypatch.setattr(cache_mod.time, "monotonic", lambda: clock["now"])
    key = _key("c")
    jobs_list_cache_set(key, {"v": 1})
    assert jobs_list_cache_get(key) == {"v": 1}
    clock["now"] += cache_mod.JOBS_LIST_CACHE_TTL_SECONDS + 0.01
    assert jobs_list_cache_get(key) is None


def test_lru_eviction_bound() -> None:
    for i in range(JOBS_LIST_CACHE_MAX_ENTRIES + 25):
        jobs_list_cache_set(_key(f"k{i}"), {"i": i})
    assert len(cache_mod._JOBS_LIST_CACHE) <= JOBS_LIST_CACHE_MAX_ENTRIES


def test_concurrent_set_get_no_crash() -> None:
    errors: list[BaseException] = []

    def worker(n: int) -> None:
        try:
            for i in range(200):
                key = _key(f"t{n}-{i % 30}")
                jobs_list_cache_set(key, {"n": n, "i": i})
                jobs_list_cache_get(key)
        except BaseException as exc:  # record for assertion
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(cache_mod._JOBS_LIST_CACHE) <= JOBS_LIST_CACHE_MAX_ENTRIES
