"""Unit tests for the BLAST database catalogue listing cache.

Responsibility: Verify ``list_databases_cached`` caches per account, returns
isolated mutable copies, invalidates per account / globally, and survives an
enumeration failure without caching the degraded result.
Edit boundaries: Pure in-process cache behaviour; never reach Azure. The
underlying enumeration is monkeypatched at the ``storage.data`` facade so this
mirrors how the route and other tests stub it.
Key entry points: the ``test_*`` functions below.
Risky contracts: Cache hits must return a fresh mutable list so the route's
``warmup_plan`` enrichment cannot corrupt the shared entry.
Validation: ``uv run pytest -q api/tests/test_database_catalog_cache.py``.
"""

from __future__ import annotations

from typing import Any

import pytest
from api.services.storage import database_catalog_cache as cat


@pytest.fixture(autouse=True)
def _reset_cache() -> Any:
    cat._reset_blast_db_listing_cache()
    yield
    cat._reset_blast_db_listing_cache()


def _install_counter(monkeypatch: pytest.MonkeyPatch, rows: list[dict[str, Any]]) -> list[int]:
    """Patch the facade enumeration and return a single-element call counter."""
    calls = [0]

    def _fake(_cred: Any, _account: str, _container: str = "blast-db") -> list[dict[str, Any]]:
        calls[0] += 1
        import copy

        return copy.deepcopy(rows)

    monkeypatch.setattr("api.services.storage.data.list_databases", _fake, raising=True)
    return calls


def test_second_call_hits_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_counter(monkeypatch, [{"name": "core_nt", "total_bytes": 1}])

    first = cat.list_databases_cached(object(), "stfake")
    second = cat.list_databases_cached(object(), "stfake")

    assert first == second == [{"name": "core_nt", "total_bytes": 1}]
    assert calls[0] == 1, "second call must be served from cache"


def test_hit_returns_isolated_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_counter(monkeypatch, [{"name": "core_nt", "total_bytes": 1}])

    first = cat.list_databases_cached(object(), "stfake")
    # Mutate the returned list the way the route's warmup_plan enrichment does.
    first[0]["warmup_plan"] = {"feasible": True}

    second = cat.list_databases_cached(object(), "stfake")
    assert "warmup_plan" not in second[0], "cache entry must not be mutated by callers"


def test_distinct_accounts_are_separate_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_counter(monkeypatch, [{"name": "core_nt"}])

    cat.list_databases_cached(object(), "stone")
    cat.list_databases_cached(object(), "sttwo")
    assert calls[0] == 2, "each account is enumerated once"

    cat.list_databases_cached(object(), "stone")
    assert calls[0] == 2, "repeat account read is cached"


def test_invalidate_specific_account(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_counter(monkeypatch, [{"name": "core_nt"}])

    cat.list_databases_cached(object(), "stone")
    cat.list_databases_cached(object(), "sttwo")
    assert calls[0] == 2

    removed = cat.invalidate_blast_db_listing_cache("stone")
    assert removed == 1

    cat.list_databases_cached(object(), "stone")  # cold again
    cat.list_databases_cached(object(), "sttwo")  # still cached
    assert calls[0] == 3


def test_invalidate_all(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_counter(monkeypatch, [{"name": "core_nt"}])

    cat.list_databases_cached(object(), "stone")
    cat.list_databases_cached(object(), "sttwo")
    assert calls[0] == 2

    removed = cat.invalidate_blast_db_listing_cache()
    assert removed == 2

    cat.list_databases_cached(object(), "stone")
    cat.list_databases_cached(object(), "sttwo")
    assert calls[0] == 4


def test_empty_account_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_counter(monkeypatch, [])

    cat.list_databases_cached(object(), "")
    cat.list_databases_cached(object(), "")
    assert calls[0] == 2, "the no-account degraded path delegates straight through"


def test_enumeration_failure_propagates_and_is_not_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = [0]

    def _boom(_cred: Any, _account: str, _container: str = "blast-db") -> list[dict[str, Any]]:
        calls[0] += 1
        raise RuntimeError("storage down")

    monkeypatch.setattr("api.services.storage.data.list_databases", _boom, raising=True)

    with pytest.raises(RuntimeError):
        cat.list_databases_cached(object(), "stfake")
    with pytest.raises(RuntimeError):
        cat.list_databases_cached(object(), "stfake")
    assert calls[0] == 2, "a failed enumeration must not be pinned in the cache"


def test_ttl_expiry_triggers_refetch(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_counter(monkeypatch, [{"name": "core_nt"}])

    # Force an already-expired TTL so the next read is a miss.
    monkeypatch.setattr(cat, "_CACHE_TTL_SECONDS", -1.0, raising=True)

    cat.list_databases_cached(object(), "stfake")
    cat.list_databases_cached(object(), "stfake")
    assert calls[0] == 2, "an expired entry must be re-enumerated"


def test_invalidation_during_fill_is_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """An invalidation that lands while the leader enumerates must win.

    The leader reads a pre-change snapshot; committing it would pin stale data
    for the whole TTL (the classic invalidate-races-cold-fill bug). The epoch
    guard makes the leader decline to cache, so the caller still gets the
    snapshot but the next read re-enumerates.
    """
    calls = [0]

    def _fake(_cred: Any, _account: str, _container: str = "blast-db") -> list[dict[str, Any]]:
        calls[0] += 1
        # Simulate an admin deleting a DB mid-enumeration: a concurrent
        # invalidation bumps this account's epoch while we are "reading".
        cat.invalidate_blast_db_listing_cache("stfake")
        return [{"name": "core_nt"}]

    monkeypatch.setattr("api.services.storage.data.list_databases", _fake, raising=True)

    first = cat.list_databases_cached(object(), "stfake")
    assert first == [{"name": "core_nt"}], "caller still receives the leader's result"
    assert calls[0] == 1

    # Because the fill was not cached, the next read re-enumerates instead of
    # serving the stale snapshot.
    cat.list_databases_cached(object(), "stfake")
    assert calls[0] == 2, "a fill invalidated mid-flight must not be pinned"


def test_force_refresh_bypasses_and_refreshes_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_counter(monkeypatch, [{"name": "core_nt"}])

    cat.list_databases_cached(object(), "stfake")  # miss -> enumerate (1)
    cat.list_databases_cached(object(), "stfake")  # hit, still 1
    assert calls[0] == 1

    cat.list_databases_cached(object(), "stfake", force_refresh=True)  # bypass -> (2)
    assert calls[0] == 2, "force_refresh must re-enumerate even on a warm cache"

    # force_refresh also repopulates the cache, so the next normal read hits.
    cat.list_databases_cached(object(), "stfake")
    assert calls[0] == 2, "force_refresh must refresh the shared cache entry"


def test_force_refresh_during_invalidation_is_not_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = [0]

    def _fake(_cred: Any, _account: str, _container: str = "blast-db") -> list[dict[str, Any]]:
        calls[0] += 1
        cat.invalidate_blast_db_listing_cache("stfake")
        return [{"name": "core_nt"}]

    monkeypatch.setattr("api.services.storage.data.list_databases", _fake, raising=True)

    cat.list_databases_cached(object(), "stfake", force_refresh=True)
    assert calls[0] == 1

    # The concurrent invalidation means the force_refresh result must not be
    # pinned either; the next read re-enumerates.
    cat.list_databases_cached(object(), "stfake")
    assert calls[0] == 2
