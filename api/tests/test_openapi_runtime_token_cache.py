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
