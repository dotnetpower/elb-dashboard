"""Tests for the per-cluster OpenAPI API token runtime cache keying.

Responsibility: Tests for per-cluster vs legacy-global OpenAPI token cache keys
Edit boundaries: Keep assertions focused on `save_openapi_api_token` /
`get_openapi_api_token` key selection and cross-cluster isolation.
Key entry points: `FakeRedis`, `test_per_cluster_token_isolation`,
`test_get_falls_back_to_global_key`
Risky contracts: Do not require a real Redis; use the injected `client=` hook.
Validation: `uv run pytest -q api/tests/test_openapi_runtime_token_cache.py`.
"""

from __future__ import annotations

from api.services.openapi import runtime


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def set(self, key: str, value: str) -> None:
        self.store[key] = value

    def get(self, key: str) -> str | None:
        return self.store.get(key)


_CLUSTER_A = {
    "subscription_id": "sub-1",
    "resource_group": "rg-a",
    "cluster_name": "aks-a",
}
_CLUSTER_B = {
    "subscription_id": "sub-1",
    "resource_group": "rg-b",
    "cluster_name": "aks-b",
}


def test_save_writes_both_global_and_per_cluster_keys() -> None:
    client = FakeRedis()
    ok = runtime.save_openapi_api_token("tok-a", metadata=_CLUSTER_A, client=client)
    assert ok is True
    # Legacy global key plus exactly one per-cluster key.
    assert runtime._TOKEN_KEY in client.store
    cluster_keys = [k for k in client.store if k.startswith(runtime._TOKEN_CLUSTER_PREFIX)]
    assert len(cluster_keys) == 1


def test_per_cluster_token_isolation() -> None:
    """Two clusters in the same revision must not contaminate each other:
    after writing A then B, reading each cluster's context returns ITS own
    token even though the global key now holds the most-recent (B)."""
    client = FakeRedis()
    runtime.save_openapi_api_token("tok-a", metadata=_CLUSTER_A, client=client)
    runtime.save_openapi_api_token("tok-b", metadata=_CLUSTER_B, client=client)

    assert runtime.get_openapi_api_token(client=client, **_CLUSTER_A) == "tok-a"
    assert runtime.get_openapi_api_token(client=client, **_CLUSTER_B) == "tok-b"
    # The context-less read returns the most-recently-written (global) token.
    assert runtime.get_openapi_api_token(client=client) == "tok-b"


def test_get_falls_back_to_global_key_when_no_per_cluster_entry() -> None:
    """A token minted before per-cluster keying landed only lives under the
    global key; a context-carrying read must still find it."""
    client = FakeRedis()
    # Simulate a legacy write: global key only (no metadata → no cluster key).
    runtime.save_openapi_api_token("legacy-tok", metadata={}, client=client)
    assert [k for k in client.store if k.startswith(runtime._TOKEN_CLUSTER_PREFIX)] == []

    assert runtime.get_openapi_api_token(client=client, **_CLUSTER_A) == "legacy-tok"


def test_get_prefers_per_cluster_over_global() -> None:
    client = FakeRedis()
    # Global says B, per-cluster A says A. Context A must win.
    runtime.save_openapi_api_token("tok-a", metadata=_CLUSTER_A, client=client)
    runtime.save_openapi_api_token("tok-b", metadata=_CLUSTER_B, client=client)
    assert client.store[runtime._TOKEN_KEY]  # global = tok-b
    assert runtime.get_openapi_api_token(client=client, **_CLUSTER_A) == "tok-a"


def test_empty_token_is_not_written() -> None:
    client = FakeRedis()
    assert runtime.save_openapi_api_token("", metadata=_CLUSTER_A, client=client) is False
    assert client.store == {}


def test_token_cluster_key_is_deterministic_and_case_insensitive() -> None:
    upper = {
        "subscription_id": "SUB-1",
        "resource_group": "RG-A",
        "cluster_name": "AKS-A",
    }
    assert runtime._token_cluster_key(_CLUSTER_A) == runtime._token_cluster_key(upper)
    assert runtime._token_cluster_key({}) == ""


def test_read_token_key_handles_plain_string_payload() -> None:
    client = FakeRedis()
    client.store[runtime._TOKEN_KEY] = "raw-token-no-json"
    assert runtime.get_openapi_api_token(client=client) == "raw-token-no-json"


# ---------------------------------------------------------------------------
# Durable cold-read rehydration (issue #49)
# ---------------------------------------------------------------------------


def _patch_durable_token(monkeypatch, store: dict) -> None:
    """Patch singleton helpers so no real Azure Table Storage is needed."""
    import api.services.state.singletons as singletons

    def fake_save(key: str, payload: dict) -> bool:
        store[key] = dict(payload)
        return True

    def fake_load(key: str) -> dict | None:
        row = store.get(key)
        return dict(row) if row is not None else None

    monkeypatch.setattr(singletons, "save_singleton", fake_save)
    monkeypatch.setattr(singletons, "load_singleton", fake_load)


def test_cold_read_rehydrates_token_from_durable_on_redis_miss(monkeypatch) -> None:
    """When Redis is empty (revision restart), get_openapi_api_token falls back
    to the durable Storage Table copy and re-populates Redis (issue #49)."""
    import time

    durable: dict = {}
    _patch_durable_token(monkeypatch, durable)
    client = FakeRedis()

    # Simulate save_openapi_api_token having written to the durable store
    # (without touching Redis — simulating the empty-Redis-after-restart state).
    fresh_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    durable[runtime._TOKEN_KEY] = {"token": "revived-tok", "updated_at": fresh_ts, "metadata": {}}

    # Redis is empty — cold path must rehydrate.
    result = runtime.get_openapi_api_token(client=client)
    assert result == "revived-tok"
    # Redis must now be re-populated so subsequent reads are hot.
    assert runtime._TOKEN_KEY in client.store


def test_cold_read_ignores_stale_durable_token(monkeypatch) -> None:
    """A durable token older than the max-age TTL must NOT be served."""
    import time

    durable: dict = {}
    _patch_durable_token(monkeypatch, durable)
    client = FakeRedis()

    stale_ts = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 7200)  # 2 hours ago
    )
    durable[runtime._TOKEN_KEY] = {"token": "stale-tok", "updated_at": stale_ts, "metadata": {}}

    result = runtime.get_openapi_api_token(client=client)
    assert result == ""


def test_cold_read_skipped_when_max_age_zero(monkeypatch) -> None:
    """Setting OPENAPI_RUNTIME_ENDPOINT_MAX_AGE_SECONDS=0 disables cold-reads
    for both the base-url and the token."""
    import time

    monkeypatch.setenv("OPENAPI_RUNTIME_ENDPOINT_MAX_AGE_SECONDS", "0")
    durable: dict = {}
    _patch_durable_token(monkeypatch, durable)
    client = FakeRedis()

    fresh_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    durable[runtime._TOKEN_KEY] = {"token": "would-revive", "updated_at": fresh_ts, "metadata": {}}

    result = runtime.get_openapi_api_token(client=client)
    assert result == ""


def test_cold_read_does_not_touch_durable_when_redis_has_token(monkeypatch) -> None:
    """When Redis already has the token, the durable store must NOT be read."""
    loads: list[str] = []
    import api.services.state.singletons as singletons

    monkeypatch.setattr(singletons, "save_singleton", lambda *a, **k: True)
    monkeypatch.setattr(
        singletons, "load_singleton", lambda key: loads.append(key) or None
    )
    client = FakeRedis()
    runtime.save_openapi_api_token("existing-tok", metadata={}, client=client)

    result = runtime.get_openapi_api_token(client=client)
    assert result == "existing-tok"
    assert loads == []  # durable never touched
