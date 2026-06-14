"""Tests for the durable backing of the IP-based OpenAPI runtime endpoint cache.

Responsibility: Verify ``save_openapi_base_url`` mirrors into the durable
singleton store and ``get_openapi_base_url`` rehydrates from it (freshness-gated)
after the in-revision Redis is wiped by a Container App revision restart.
Edit boundaries: Keep assertions focused on the Redis ↔ durable interplay and
the freshness TTL guard; use the injected ``client=`` Redis hook and monkeypatch
the singleton store.
Key entry points: ``FakeRedis``, ``test_save_writes_redis_and_durable``,
``test_cold_read_rehydrates_from_durable_when_fresh``,
``test_cold_read_ignores_stale_durable``.
Risky contracts: Do not require a real Redis or Azure Table; inject ``client=``
and patch ``api.services.state.singletons``.
Validation: ``uv run pytest -q api/tests/test_openapi_runtime_endpoint_durable.py``.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from api.services.openapi import runtime


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def set(self, key: str, value: str) -> None:
        self.store[key] = value

    def get(self, key: str) -> str | None:
        return self.store.get(key)


def _patch_durable(monkeypatch, store: dict[str, dict[str, Any]]) -> None:
    """Patch the singleton helpers ``runtime`` imports lazily inside functions."""
    import api.services.state.singletons as singletons

    def fake_save(key: str, payload: dict[str, Any]) -> bool:
        store[key] = dict(payload)
        return True

    def fake_load(key: str) -> dict[str, Any] | None:
        row = store.get(key)
        return dict(row) if row is not None else None

    monkeypatch.setattr(singletons, "save_singleton", fake_save)
    monkeypatch.setattr(singletons, "load_singleton", fake_load)


def test_save_writes_redis_and_durable(monkeypatch) -> None:
    durable: dict[str, dict[str, Any]] = {}
    _patch_durable(monkeypatch, durable)
    client = FakeRedis()

    ok = runtime.save_openapi_base_url(
        "http://10.0.0.5",
        metadata={"cluster_name": "aks-a"},
        client=client,
    )

    assert ok is True
    assert runtime._RUNTIME_KEY in client.store
    # Durable mirror written too, so a revision restart can rehydrate it.
    assert runtime._RUNTIME_KEY in durable
    assert durable[runtime._RUNTIME_KEY]["base_url"] == "http://10.0.0.5"


def test_redis_hit_does_not_touch_durable(monkeypatch) -> None:
    loads: list[str] = []
    import api.services.state.singletons as singletons

    monkeypatch.setattr(singletons, "save_singleton", lambda *_a, **_k: True)
    monkeypatch.setattr(
        singletons, "load_singleton", lambda key: loads.append(key) or None
    )
    client = FakeRedis()
    runtime.save_openapi_base_url("http://10.0.0.5", client=client)

    assert runtime.get_openapi_base_url(client=client) == "http://10.0.0.5"
    # A Redis hit must not pay a durable read.
    assert loads == []


def test_cold_read_rehydrates_from_durable_when_fresh(monkeypatch) -> None:
    durable: dict[str, dict[str, Any]] = {}
    _patch_durable(monkeypatch, durable)

    # Simulate a prior save (writes both Redis + durable).
    warm = FakeRedis()
    runtime.save_openapi_base_url("http://10.0.0.7", client=warm)

    # New revision: Redis is empty, durable survives.
    cold = FakeRedis()
    url = runtime.get_openapi_base_url(client=cold)

    assert url == "http://10.0.0.7"
    # The cold read re-populated Redis so subsequent reads are hot again.
    assert runtime._RUNTIME_KEY in cold.store


def test_cold_read_ignores_stale_durable(monkeypatch) -> None:
    durable: dict[str, dict[str, Any]] = {
        runtime._RUNTIME_KEY: {
            "base_url": "http://10.0.0.9",
            "metadata": {},
            # Two hours old → older than the 1 h default max-age.
            "updated_at": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 7200)
            ),
        }
    }
    _patch_durable(monkeypatch, durable)
    cold = FakeRedis()

    assert runtime.get_openapi_base_url(client=cold) == ""
    # Stale durable row is not re-cached into Redis.
    assert runtime._RUNTIME_KEY not in cold.store


def test_cold_read_ignores_undatable_durable(monkeypatch) -> None:
    durable: dict[str, dict[str, Any]] = {
        runtime._RUNTIME_KEY: {"base_url": "http://10.0.0.11", "metadata": {}}
    }
    _patch_durable(monkeypatch, durable)
    cold = FakeRedis()

    # No updated_at → fail-closed (treated as not fresh).
    assert runtime.get_openapi_base_url(client=cold) == ""


def test_cold_read_disabled_when_max_age_non_positive(monkeypatch) -> None:
    monkeypatch.setenv(runtime._RUNTIME_ENDPOINT_MAX_AGE_ENV, "0")
    loads: list[str] = []
    import api.services.state.singletons as singletons

    monkeypatch.setattr(
        singletons, "load_singleton", lambda key: loads.append(key) or None
    )
    cold = FakeRedis()

    assert runtime.get_openapi_base_url(client=cold) == ""
    # Disabled freshness window short-circuits before any durable read.
    assert loads == []


def test_durable_read_failure_degrades_to_empty(monkeypatch) -> None:
    import api.services.state.singletons as singletons

    def boom(_key: str) -> dict[str, Any] | None:
        raise RuntimeError("table unavailable")

    monkeypatch.setattr(singletons, "load_singleton", boom)
    cold = FakeRedis()

    # A durable-read failure must never raise — degrade to "".
    assert runtime.get_openapi_base_url(client=cold) == ""


@pytest.mark.parametrize("override", ["", "not-a-number"])
def test_max_age_override_falls_back_to_default(monkeypatch, override: str) -> None:
    monkeypatch.setenv(runtime._RUNTIME_ENDPOINT_MAX_AGE_ENV, override)
    assert runtime._runtime_endpoint_max_age() == runtime._RUNTIME_ENDPOINT_MAX_AGE_DEFAULT
