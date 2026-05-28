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
