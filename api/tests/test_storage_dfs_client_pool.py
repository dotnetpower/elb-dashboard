"""Regression tests for the DataLakeServiceClient (dfs) pool + feature gate.

Responsibility: Prove the dfs pool mirrors the proven BlobServiceClient pool —
    deadlock-safe credential finalizer, deferred-eviction drain, LRU eviction,
    thread-local fast path, reset — and that ``dfs_enabled()`` defaults OFF and
    only flips ON for the documented truthy values.
Edit boundaries: Pool lifecycle / flag behaviour only. No network or real Azure
    credentials — the credential is a bare object and the pooled client is never
    used for I/O.
Key entry points: ``test_dfs_enabled_*``, ``test_finalizer_defers_*``,
    ``test_pending_eviction_drained_*``, ``test_lru_eviction_closes_oldest``.
Risky contracts: ``_evict_credential_or_defer`` MUST acquire the pool lock
    non-blocking; a blocking acquire reintroduces a self-deadlock.
Validation: ``uv run pytest -q api/tests/test_storage_dfs_client_pool.py``.
"""

from __future__ import annotations

import pytest
from api.services.storage import dfs_client_pool


class _FakeDfsServiceClient:
    """Stand-in for the real client — accepts any credential, no network."""

    def __init__(self, **_kwargs: object) -> None:
        self.closed = False

    def get_file_system_client(self, filesystem: str) -> object:
        return ("fs", filesystem)

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _clean_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    # Patch construction so the pool never builds a real (network-capable)
    # client; the pool/flag logic is independent of the SDK object.
    monkeypatch.setattr(dfs_client_pool, "DataLakeServiceClient", _FakeDfsServiceClient)
    dfs_client_pool.reset_dfs_service_pool()
    yield
    dfs_client_pool.reset_dfs_service_pool()


class _Cred:
    """A bare credential object weakref.finalize can attach to."""


# --- feature flag ----------------------------------------------------------


def test_dfs_enabled_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STORAGE_DFS_ENABLED", raising=False)
    assert dfs_client_pool.dfs_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " On "])
def test_dfs_enabled_truthy(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("STORAGE_DFS_ENABLED", value)
    assert dfs_client_pool.dfs_enabled() is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "maybe"])
def test_dfs_enabled_falsy(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("STORAGE_DFS_ENABLED", value)
    assert dfs_client_pool.dfs_enabled() is False


# --- account-name validation ----------------------------------------------


@pytest.mark.parametrize("bad", ["", "UPPER", "has-dash", "x" * 25, "ab", "a/b"])
def test_invalid_account_name_rejected(bad: str) -> None:
    with pytest.raises(ValueError):
        dfs_client_pool._dfs_service(_Cred(), bad)


def test_filesystem_client_derives_from_pooled_service() -> None:
    cred = _Cred()
    fs = dfs_client_pool._dfs_filesystem(cred, "elbstg01", "results")
    assert fs == ("fs", "results")


# --- pooling / reuse -------------------------------------------------------


def test_same_credential_account_is_pooled() -> None:
    cred = _Cred()
    a = dfs_client_pool._dfs_service(cred, "elbstg01")
    b = dfs_client_pool._dfs_service(cred, "elbstg01")
    assert a is b


def test_reset_closes_clients() -> None:
    cred = _Cred()
    client = dfs_client_pool._dfs_service(cred, "elbstg01")
    dfs_client_pool.reset_dfs_service_pool()
    assert client.closed is True
    assert len(dfs_client_pool._DFS_SERVICE_POOL) == 0


@pytest.mark.timeout(15)
def test_lru_eviction_closes_oldest(monkeypatch: pytest.MonkeyPatch) -> None:
    """Past the pool cap, the least-recently-used client is evicted + closed."""
    monkeypatch.setattr(dfs_client_pool, "_DFS_SERVICE_POOL_MAX", 2)
    cred = _Cred()
    first = dfs_client_pool._dfs_service(cred, "elbstg01")
    # Distinct accounts (same cred) → distinct pool keys.
    dfs_client_pool._dfs_service(cred, "elbstg02")
    # Clear the thread-local fast path so the third insert exercises the pool.
    dfs_client_pool._DFS_SERVICE_THREAD_LOCAL.cache.clear()
    dfs_client_pool._dfs_service(cred, "elbstg03")
    assert first.closed is True
    assert len(dfs_client_pool._DFS_SERVICE_POOL) == 2


# --- deadlock safety (mirrors the blob-pool regression) --------------------


@pytest.mark.timeout(15)
def test_finalizer_defers_when_pool_lock_held() -> None:
    cred = _Cred()
    dfs_client_pool._dfs_service(cred, "elbstg01")
    target_id = id(cred)
    assert target_id in dfs_client_pool._DFS_SERVICE_CREDENTIAL_FINALIZED

    with dfs_client_pool._DFS_SERVICE_POOL_LOCK:
        dfs_client_pool._evict_credential_or_defer(target_id)  # must NOT block
        assert target_id in dfs_client_pool._PENDING_CRED_EVICTIONS


@pytest.mark.timeout(15)
def test_pending_eviction_drained_by_next_pool_op() -> None:
    cred = _Cred()
    dfs_client_pool._dfs_service(cred, "elbstg01")
    target_id = id(cred)
    assert any(key[0] == target_id for key in dfs_client_pool._DFS_SERVICE_POOL)

    with dfs_client_pool._DFS_SERVICE_POOL_LOCK:
        dfs_client_pool._evict_credential_or_defer(target_id)
    assert target_id in dfs_client_pool._PENDING_CRED_EVICTIONS

    other = _Cred()
    dfs_client_pool._dfs_service(other, "elbstg01")
    assert target_id not in dfs_client_pool._PENDING_CRED_EVICTIONS
    assert not any(key[0] == target_id for key in dfs_client_pool._DFS_SERVICE_POOL)


@pytest.mark.timeout(15)
def test_direct_finalizer_eviction_pops_clients_when_lock_free() -> None:
    cred = _Cred()
    dfs_client_pool._dfs_service(cred, "elbstg01")
    target_id = id(cred)
    assert any(key[0] == target_id for key in dfs_client_pool._DFS_SERVICE_POOL)

    dfs_client_pool._evict_credential_or_defer(target_id)
    assert target_id not in dfs_client_pool._PENDING_CRED_EVICTIONS
    assert not any(key[0] == target_id for key in dfs_client_pool._DFS_SERVICE_POOL)


def test_prune_idle_evicts_old_clients() -> None:
    cred = _Cred()
    client = dfs_client_pool._dfs_service(cred, "elbstg01")
    # ttl=0 short-circuits to a no-op; a tiny negative-free cutoff evicts all.
    pruned = dfs_client_pool.prune_idle_dfs_service_clients(idle_ttl_seconds=1e-9)
    assert pruned == 1
    assert client.closed is True
