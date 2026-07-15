"""Tests for `api.services.state.singletons`.

Module docstring (natural):
Pin the small key-value store used for cross-revision singletons (the
public-HTTPS endpoint URL today). The Table client is faked so the
tests can run without Azure credentials and still cover the
save/load/clear/round-trip contracts plus the "endpoint env unset"
fallback path that local dev hits.

Responsibility: Unit tests for `save_singleton`, `load_singleton`,
    `clear_singleton`, and the `_endpoint() == ""` short-circuit.
Edit boundaries: Storage primitive tests only — no domain-level
    assertions (those live in the per-caller test modules).
Key entry points: `test_round_trip`, `test_missing_endpoint_returns_none`,
    `test_clear_removes_row`.
Risky contracts: The fake TableClient mimics the SDK contract
    `upsert_entity` / `get_entity` / `delete_entity`; the real SDK uses
    `ResourceNotFoundError` for missing rows. The store treats any
    exception on `get_entity` as a miss so the test fake can raise a
    plain `Exception`.
Validation: `uv run pytest -q api/tests/test_state_singletons.py`.
"""

from __future__ import annotations

from typing import Any

import pytest
from api.services.state import singletons


class _FakeTableClient:
    """Minimal stand-in for the Azure Tables SDK TableClient."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}

    def upsert_entity(self, entity: dict[str, Any]) -> None:
        key = (entity["PartitionKey"], entity["RowKey"])
        self.rows[key] = dict(entity)

    def get_entity(self, partition_key: str, row_key: str) -> dict[str, Any]:
        try:
            return dict(self.rows[(partition_key, row_key)])
        except KeyError as exc:
            raise Exception("ResourceNotFoundError") from exc

    def delete_entity(self, partition_key: str, row_key: str) -> None:
        self.rows.pop((partition_key, row_key), None)

    def query_entities(self, query_filter: str) -> list[dict[str, Any]]:
        """Mimic the Azure Tables OData range filter used by the store.

        Parses the ``PartitionKey eq '..' and RowKey ge '..' and RowKey lt '..'``
        shape the store emits and applies an ordinal string comparison — the
        same ordering Azure Tables uses — so the test exercises the real
        ``_prefix_upper_bound`` math rather than a wildcard.
        """
        import re

        pk = re.search(r"PartitionKey eq '([^']*)'", query_filter)
        ge = re.search(r"RowKey ge '([^']*)'", query_filter)
        lt = re.search(r"RowKey lt '([^']*)'", query_filter)
        partition = pk.group(1) if pk else None
        lower = ge.group(1) if ge else None
        upper = lt.group(1) if lt else None
        out: list[dict[str, Any]] = []
        for (row_pk, row_rk), entity in self.rows.items():
            if partition is not None and row_pk != partition:
                continue
            if lower is not None and row_rk < lower:
                continue
            if upper is not None and not row_rk < upper:
                continue
            out.append(dict(entity))
        return out

    def close(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _reset_singleton_cache_and_table(monkeypatch: pytest.MonkeyPatch):
    """Wire a fake TableClient into the module and reset its caches between tests."""
    fake = _FakeTableClient()
    monkeypatch.setattr(singletons, "_CLIENT", fake, raising=False)
    monkeypatch.setattr(singletons, "_TABLE_ENSURED", True, raising=False)
    # Anything that would normally touch Azure must short-circuit through
    # the fake — set the endpoint env so the gate passes.
    monkeypatch.setenv(singletons._TABLE_ENDPOINT_ENV, "https://fake.table.core.windows.net")
    yield fake
    singletons.reset_singleton_cache_for_tests()


def test_round_trip(_reset_singleton_cache_and_table: _FakeTableClient) -> None:
    payload = {"base_url": "https://example.com", "n": 42}
    assert singletons.save_singleton("openapi:runtime:public-base-url", payload) is True
    loaded = singletons.load_singleton("openapi:runtime:public-base-url")
    assert loaded == payload


def test_load_missing_returns_none(_reset_singleton_cache_and_table: _FakeTableClient) -> None:
    assert singletons.load_singleton("nope") is None


def test_strict_load_propagates_table_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    class _UnavailableClient:
        def get_entity(self, _partition_key: str, _row_key: str) -> dict[str, Any]:
            raise RuntimeError("table unavailable")

    monkeypatch.setattr(singletons, "_CLIENT", _UnavailableClient())

    with pytest.raises(RuntimeError, match="table unavailable"):
        singletons.load_singleton_strict("execution-admission")


def test_clear_removes_row(_reset_singleton_cache_and_table: _FakeTableClient) -> None:
    singletons.save_singleton("k1", {"a": 1})
    assert singletons.clear_singleton("k1") is True
    assert singletons.load_singleton("k1") is None


def test_row_key_is_sanitised(_reset_singleton_cache_and_table: _FakeTableClient) -> None:
    """Azure RowKey forbids `/ \\ # ?` and control chars; the helper must replace them."""
    raw = "openapi:runtime:public/base?url"
    singletons.save_singleton(raw, {"ok": True})
    sanitised_rows = list(_reset_singleton_cache_and_table.rows.keys())
    assert len(sanitised_rows) == 1
    _pk, row = sanitised_rows[0]
    assert "/" not in row and "?" not in row


def test_missing_endpoint_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    """When AZURE_TABLE_ENDPOINT is unset, every operation must no-op silently.

    Local dev paths run without Storage credentials; the durable store
    must degrade to Redis-only without raising.
    """
    monkeypatch.delenv(singletons._TABLE_ENDPOINT_ENV, raising=False)
    singletons.reset_singleton_cache_for_tests()
    assert singletons.save_singleton("k", {"a": 1}) is False
    assert singletons.load_singleton("k") is None


def test_list_by_prefix_returns_matching_rows(
    _reset_singleton_cache_and_table: _FakeTableClient,
) -> None:
    """Every row under the prefix is returned; rows outside it are excluded."""
    singletons.save_singleton("openapi:runtime:public-base-url:cluster:aaaa", {"n": 1})
    singletons.save_singleton("openapi:runtime:public-base-url:cluster:bbbb", {"n": 2})
    # Global key (no `:cluster:` suffix) — must NOT be under the cluster prefix.
    singletons.save_singleton("openapi:runtime:public-base-url", {"n": 0})
    singletons.save_singleton("unrelated:key", {"n": 9})

    rows = singletons.list_singletons_by_prefix(
        "openapi:runtime:public-base-url:cluster:"
    )
    keys = sorted(rk for rk, _payload in rows)
    assert keys == [
        "openapi:runtime:public-base-url:cluster:aaaa",
        "openapi:runtime:public-base-url:cluster:bbbb",
    ]


def test_list_by_prefix_includes_suffix_above_tilde(
    _reset_singleton_cache_and_table: _FakeTableClient,
) -> None:
    """A suffix sorting at/above a fixed `~` sentinel must still be returned.

    Regression for the old `prefix + "~~~~~~~~"` upper bound, which silently
    dropped any key whose suffix sorted at/above the sentinel. The prefix here
    ends in `:`, and RowKeys may legitimately contain `~` (0x7e) or characters
    above it (the sanitiser allows >= 0x00a0), so the bound must be derived from
    the prefix, not a fixed trailing sentinel.
    """
    singletons.save_singleton("p:~~~~~~~~~more", {"n": 1})  # 9 tildes — past an 8-tilde sentinel
    singletons.save_singleton("p:\u00e9-accented", {"n": 2})  # suffix char above 0x7e

    rows = singletons.list_singletons_by_prefix("p:")
    keys = sorted(rk for rk, _payload in rows)
    assert keys == ["p:~~~~~~~~~more", "p:\u00e9-accented"]


def test_list_by_prefix_empty_prefix_returns_empty(
    _reset_singleton_cache_and_table: _FakeTableClient,
) -> None:
    singletons.save_singleton("anything", {"n": 1})
    assert singletons.list_singletons_by_prefix("") == []

