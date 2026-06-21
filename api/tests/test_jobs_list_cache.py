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
    begin_jobs_list_revalidate,
    end_jobs_list_revalidate,
    jobs_list_cache_get,
    jobs_list_cache_get_swr,
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


def test_cursor_distinguishes_cache_key() -> None:
    """Each cursor page is a distinct cache entry so a paginated request never
    serves another page's payload (#50/#51 C10)."""
    base = dict(
        caller_oid="a",
        limit=50,
        subscription_id="sub",
        resource_group="rg",
        cluster_name="cl",
        shared_visibility=False,
    )
    first_page = jobs_list_cache_key(**base)
    same_first = jobs_list_cache_key(**base, cursor="")
    second_page = jobs_list_cache_key(**base, cursor="CURSOR_PAGE_2")
    third_page = jobs_list_cache_key(**base, cursor="CURSOR_PAGE_3")

    # Empty cursor is the default → identical to the no-cursor call (back-compat).
    assert first_page == same_first
    # Distinct cursors → distinct keys, so pages cannot collide in the cache.
    assert len({first_page, second_page, third_page}) == 3


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


def test_swr_fresh_then_stale_then_cold(monkeypatch) -> None:
    """jobs_list_cache_get_swr must report fresh, then stale, then cold."""
    clock = {"now": 1000.0}
    monkeypatch.setattr(cache_mod.time, "monotonic", lambda: clock["now"])
    key = _key("swr")
    jobs_list_cache_set(key, {"v": 1})

    payload, is_stale = jobs_list_cache_get_swr(key)
    assert payload == {"v": 1} and is_stale is False

    # Past the fresh window but inside the stale ceiling → stale (still served).
    clock["now"] += cache_mod.JOBS_LIST_CACHE_TTL_SECONDS + 0.01
    payload, is_stale = jobs_list_cache_get_swr(key)
    assert payload == {"v": 1} and is_stale is True

    # Past the stale ceiling → cold (dropped).
    clock["now"] += cache_mod.JOBS_LIST_CACHE_STALE_TTL_SECONDS
    payload, is_stale = jobs_list_cache_get_swr(key)
    assert payload is None and is_stale is False


def test_legacy_get_is_fresh_only(monkeypatch) -> None:
    """The legacy strict-freshness reader must return None once stale."""
    clock = {"now": 1000.0}
    monkeypatch.setattr(cache_mod.time, "monotonic", lambda: clock["now"])
    key = _key("legacy")
    jobs_list_cache_set(key, {"v": 1})
    assert jobs_list_cache_get(key) == {"v": 1}
    clock["now"] += cache_mod.JOBS_LIST_CACHE_TTL_SECONDS + 0.01
    # Stale to SWR, but the legacy reader treats stale as a miss.
    assert jobs_list_cache_get(key) is None
    payload, is_stale = jobs_list_cache_get_swr(key)
    assert payload == {"v": 1} and is_stale is True


def test_revalidate_single_flight() -> None:
    """Only one revalidation slot per key until it is released."""
    key = _key("rv")
    assert begin_jobs_list_revalidate(key) is True
    # A second begin while the first is in flight is denied.
    assert begin_jobs_list_revalidate(key) is False
    end_jobs_list_revalidate(key)
    # Slot is free again.
    assert begin_jobs_list_revalidate(key) is True
    end_jobs_list_revalidate(key)


def test_revalidate_slot_reelects_after_ttl(monkeypatch) -> None:
    """A leader that never released (crash) must not wedge revalidation: a
    later begin past the inflight TTL re-elects."""
    clock = {"now": 5000.0}
    monkeypatch.setattr(cache_mod.time, "monotonic", lambda: clock["now"])
    key = _key("rv-ttl")
    assert begin_jobs_list_revalidate(key) is True
    assert begin_jobs_list_revalidate(key) is False
    clock["now"] += cache_mod._JOBS_LIST_REVALIDATE_TTL_SECONDS + 0.01
    assert begin_jobs_list_revalidate(key) is True
    end_jobs_list_revalidate(key)


def test_swr_retains_entry_while_revalidation_inflight(monkeypatch) -> None:
    """Past the stale ceiling, an entry is RETAINED (served stale) while a
    revalidation is in flight, instead of dropping to cold and forcing the next
    poll down the blocking synchronous build path."""
    clock = {"now": 1000.0}
    monkeypatch.setattr(cache_mod.time, "monotonic", lambda: clock["now"])
    key = _key("swr-inflight")
    jobs_list_cache_set(key, {"v": 1})

    # A rebuild claims the single-flight slot.
    assert begin_jobs_list_revalidate(key) is True

    # Jump well past the stale ceiling. Because a revalidation is in flight the
    # entry is retained and served as stale (not dropped to cold).
    clock["now"] += cache_mod.JOBS_LIST_CACHE_STALE_TTL_SECONDS + 5.0
    payload, is_stale = jobs_list_cache_get_swr(key)
    assert payload == {"v": 1} and is_stale is True
    # The entry is still present.
    assert key in cache_mod._JOBS_LIST_CACHE

    # Once the rebuild releases the slot, a past-ceiling read drops to cold.
    end_jobs_list_revalidate(key)
    payload, is_stale = jobs_list_cache_get_swr(key)
    assert payload is None and is_stale is False
    assert key not in cache_mod._JOBS_LIST_CACHE


def test_swr_inflight_retention_expires_with_slot_ttl(monkeypatch) -> None:
    """Retention is bounded by the revalidate TTL: a leader that crashed without
    releasing the slot stops retaining the entry once the slot TTL lapses, so a
    genuinely wedged rebuild cannot pin a stale entry forever."""
    clock = {"now": 2000.0}
    monkeypatch.setattr(cache_mod.time, "monotonic", lambda: clock["now"])
    key = _key("swr-inflight-ttl")
    jobs_list_cache_set(key, {"v": 1})
    assert begin_jobs_list_revalidate(key) is True

    # Past the stale ceiling but the slot is still within its TTL → retained.
    clock["now"] += cache_mod.JOBS_LIST_CACHE_STALE_TTL_SECONDS + 1.0
    payload, is_stale = jobs_list_cache_get_swr(key)
    assert payload == {"v": 1} and is_stale is True

    # Advance beyond the revalidate TTL (the crashed-leader window) → no longer
    # retained, entry drops to cold.
    clock["now"] += cache_mod._JOBS_LIST_REVALIDATE_TTL_SECONDS
    payload, is_stale = jobs_list_cache_get_swr(key)
    assert payload is None and is_stale is False


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
