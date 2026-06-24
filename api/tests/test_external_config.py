"""Tests for the external/queue BLAST config_snapshot + region enrichment helper.

Responsibility: cover snapshot construction (key filtering, empty input), the
  cached region resolver (cache hit / miss / failure → ""), and the remember /
  recall round-trip with a fake OPS Redis.
Edit boundaries: test-only; exercises ``api.services.blast.external_config``.
Validation: ``uv run pytest -q api/tests/test_external_config.py``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from api.services.blast import external_config as ec


def test_build_snapshot_filters_known_keys_and_drops_empty() -> None:
    snap = ec.build_external_config_snapshot(
        {
            "outfmt": "7 std staxids",
            "evalue": 0.05,
            "word_size": 28,
            "dust": "yes",
            "max_target_seqs": 500,
            "taxid": 3431483,
            "is_inclusive": False,
            "unknown_field": "ignored",
            "matrix": "",  # empty dropped
            "gap_open": None,  # None dropped
        }
    )
    assert snap["outfmt"] == "7 std staxids"
    assert snap["evalue"] == 0.05
    assert snap["word_size"] == 28
    assert snap["max_target_seqs"] == 500
    assert snap["taxid"] == 3431483
    assert snap["is_inclusive"] is False
    assert "unknown_field" not in snap
    assert "matrix" not in snap
    assert "gap_open" not in snap


def test_build_snapshot_empty_inputs() -> None:
    assert ec.build_external_config_snapshot(None) == {}
    assert ec.build_external_config_snapshot({}) == {}
    assert ec.build_external_config_snapshot("not a dict") == {}  # type: ignore[arg-type]


def test_build_snapshot_caps_free_form_options() -> None:
    snap = ec.build_external_config_snapshot(
        {"additional_options": "-outfmt " + "x" * 5000, "extra": "y" * 5000}
    )
    assert len(snap["additional_options"]) == ec._FREE_FORM_MAX_LEN
    assert len(snap["extra"]) == ec._FREE_FORM_MAX_LEN


def test_build_snapshot_includes_taxonomy_keys() -> None:
    snap = ec.build_external_config_snapshot(
        {"taxids": "9606", "negative_taxids": "3431483"}
    )
    assert snap["taxids"] == "9606"
    assert snap["negative_taxids"] == "3431483"


def test_resolve_cluster_region_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    ec.reset_region_cache_for_test()
    calls = {"n": 0}

    def fake_snapshot(_cred: Any, _sub: str, _rg: str, _name: str) -> dict[str, Any]:
        calls["n"] += 1
        return {"region": "koreacentral"}

    monkeypatch.setattr("api.services.get_credential", lambda: object(), raising=False)
    monkeypatch.setattr(
        "api.services.monitoring.get_aks_cluster_snapshot", fake_snapshot, raising=False
    )

    r1 = ec.resolve_cluster_region("sub", "rg", "elb-cluster-01")
    r2 = ec.resolve_cluster_region("sub", "rg", "elb-cluster-01")
    assert r1 == "koreacentral"
    assert r2 == "koreacentral"
    assert calls["n"] == 1  # second call served from cache


def test_resolve_cluster_region_blank_args_and_failure() -> None:
    ec.reset_region_cache_for_test()
    assert ec.resolve_cluster_region("", "rg", "c") == ""
    assert ec.resolve_cluster_region("sub", "", "c") == ""
    assert ec.resolve_cluster_region("sub", "rg", "") == ""


def test_resolve_cluster_region_failure_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ec.reset_region_cache_for_test()

    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("aks unreachable")

    monkeypatch.setattr("api.services.get_credential", lambda: object(), raising=False)
    monkeypatch.setattr(
        "api.services.monitoring.get_aks_cluster_snapshot", boom, raising=False
    )
    assert ec.resolve_cluster_region("sub", "rg", "c") == ""


def test_region_negative_cache_is_short(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed resolve is cached only briefly so AKS recovery is picked up fast."""
    ec.reset_region_cache_for_test()
    monkeypatch.setattr("api.services.get_credential", lambda: object(), raising=False)
    monkeypatch.setattr(
        "api.services.monitoring.get_aks_cluster_snapshot",
        lambda *a, **k: {"region": ""},
        raising=False,
    )
    ec.resolve_cluster_region("sub", "rg", "c")
    key = "sub/rg/c"
    expiry, value = ec._REGION_CACHE[key]
    assert value == ""
    # Negative entry expires far sooner than a resolved one.
    import time as _t

    assert expiry - _t.monotonic() <= ec._REGION_NEGATIVE_TTL_SECONDS + 1


def test_region_cache_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    ec.reset_region_cache_for_test()
    monkeypatch.setattr("api.services.get_credential", lambda: object(), raising=False)
    monkeypatch.setattr(
        "api.services.monitoring.get_aks_cluster_snapshot",
        lambda *a, **k: {"region": "koreacentral"},
        raising=False,
    )
    for i in range(ec._REGION_CACHE_MAX + 50):
        ec.resolve_cluster_region("sub", "rg", f"cluster-{i}")
    assert len(ec._REGION_CACHE) <= ec._REGION_CACHE_MAX


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.store[key] = value

    def get(self, key: str) -> str | None:
        return self.store.get(key)


def test_remember_and_recall_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeRedis()
    monkeypatch.setattr(
        "api.services.redis_clients.get_ops_redis_client", lambda: fake, raising=False
    )
    ec.remember_config_snapshot("job-1", {"outfmt": "7 std", "evalue": 0.05})
    assert ec.recall_config_snapshot("job-1") == {"outfmt": "7 std", "evalue": 0.05}
    # round-trips through JSON
    assert json.loads(fake.store[ec._REMEMBER_KEY_PREFIX + "job-1"])["outfmt"] == "7 std"


def test_recall_missing_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeRedis()
    monkeypatch.setattr(
        "api.services.redis_clients.get_ops_redis_client", lambda: fake, raising=False
    )
    assert ec.recall_config_snapshot("never") == {}
    assert ec.recall_config_snapshot("") == {}


def test_remember_skips_empty_and_oversized(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeRedis()
    monkeypatch.setattr(
        "api.services.redis_clients.get_ops_redis_client", lambda: fake, raising=False
    )
    ec.remember_config_snapshot("", {"a": 1})
    ec.remember_config_snapshot("job", {})
    ec.remember_config_snapshot("job", {"x": "y" * 5000})  # over the cap
    assert fake.store == {}


class _TtlRedis:
    """Fake OPS Redis that records the ``ex`` (TTL) passed to ``set``."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.ttl: dict[str, int | None] = {}

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.store[key] = value
        self.ttl[key] = ex

    def get(self, key: str) -> str | None:
        return self.store.get(key)


def test_sibling_stats_remember_and_recall(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _TtlRedis()
    monkeypatch.setattr(
        "api.services.redis_clients.get_ops_redis_client", lambda: fake, raising=False
    )
    ec.remember_sibling_stats("job-9", {"db_version": "2026-06-06", "run_seconds": 95})
    assert ec.recall_sibling_stats("job-9") == {
        "db_version": "2026-06-06",
        "run_seconds": 95,
    }
    # Positive stats use the long TTL.
    assert fake.ttl[ec._STATS_KEY_PREFIX + "job-9"] == ec._STATS_TTL_SECONDS


def test_sibling_stats_miss_marker_is_short_lived(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _TtlRedis()
    monkeypatch.setattr(
        "api.services.redis_clients.get_ops_redis_client", lambda: fake, raising=False
    )
    ec.remember_sibling_stats_miss("job-miss")
    # The negative marker counts as a cache HIT (truthy) so the caller skips the
    # live re-fetch, and it carries the short negative TTL so it recovers soon.
    cached = ec.recall_sibling_stats("job-miss")
    assert cached and ec._STATS_ATTEMPT_MARKER in cached
    assert fake.ttl[ec._STATS_KEY_PREFIX + "job-miss"] == int(
        ec._STATS_NEGATIVE_TTL_SECONDS
    )
    assert ec._STATS_NEGATIVE_TTL_SECONDS < ec._STATS_TTL_SECONDS


def test_sibling_stats_recall_missing_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _TtlRedis()
    monkeypatch.setattr(
        "api.services.redis_clients.get_ops_redis_client", lambda: fake, raising=False
    )
    assert ec.recall_sibling_stats("nope") == {}
    assert ec.recall_sibling_stats("") == {}
