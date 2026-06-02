"""Tests for the ARM management-client pool in api.services.azure_clients.

Module docstring (natural):
Locks in the connection-reuse behaviour added to cut repeated TLS handshakes
to management.azure.com on the polling monitor routes: one client instance per
(kind, credential identity, subscription) with credential-GC eviction, an
opt-out env flag, and a test-reset hook.

Responsibility: Cover the pooling contract (reuse, per-key isolation, env
    opt-out, reset) of `_pooled_mgmt_client` / `resource_client` /
    `reset_mgmt_client_pool` without constructing real Azure SDK clients.
Edit boundaries: Test-only. Stub the SDK class at the module level the
    factory references; never hit the network.
Key entry points: `test_resource_client_pools_per_subscription`,
    `test_pool_disabled_constructs_each_call`,
    `test_reset_mgmt_client_pool_drops_clients`.
Risky contracts: Factories build the SDK class via the module-global name, so
    monkeypatching `azure_clients.ResourceManagementClient` must be honoured on
    a cache miss.
Validation: `uv run pytest -q api/tests/test_mgmt_client_pool.py`.
"""

from __future__ import annotations

import pytest
from api.services import azure_clients


class _FakeCredential:
    """A distinct, hashable, GC-able object to key the pool on."""


class _FakeResourceClient:
    instances = 0

    def __init__(self, credential: object, subscription_id: str) -> None:
        type(self).instances += 1
        self.credential = credential
        self.subscription_id = subscription_id
        self.closed = False

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _reset_pool_and_counter(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeResourceClient.instances = 0
    monkeypatch.setattr(azure_clients, "ResourceManagementClient", _FakeResourceClient)
    azure_clients.reset_mgmt_client_pool()
    yield
    azure_clients.reset_mgmt_client_pool()


def test_resource_client_pools_per_subscription(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_MGMT_CLIENT_POOL", "true")
    cred = _FakeCredential()

    a1 = azure_clients.resource_client(cred, "sub-a")
    a2 = azure_clients.resource_client(cred, "sub-a")
    b1 = azure_clients.resource_client(cred, "sub-b")

    # Same (cred, sub) reuses one instance; a different sub builds a new one.
    assert a1 is a2
    assert b1 is not a1
    assert _FakeResourceClient.instances == 2

    # A different credential identity is a different pool key.
    other = azure_clients.resource_client(_FakeCredential(), "sub-a")
    assert other is not a1
    assert _FakeResourceClient.instances == 3


def test_pool_disabled_constructs_each_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_MGMT_CLIENT_POOL", "false")
    cred = _FakeCredential()

    c1 = azure_clients.resource_client(cred, "sub-a")
    c2 = azure_clients.resource_client(cred, "sub-a")

    assert c1 is not c2
    assert _FakeResourceClient.instances == 2


def test_reset_mgmt_client_pool_drops_and_closes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_MGMT_CLIENT_POOL", "true")
    cred = _FakeCredential()

    first = azure_clients.resource_client(cred, "sub-a")
    azure_clients.reset_mgmt_client_pool()

    # Pooled clients are closed on reset and the next call rebuilds.
    assert first.closed is True
    second = azure_clients.resource_client(cred, "sub-a")
    assert second is not first
    assert _FakeResourceClient.instances == 2
