"""Regression tests for the BlobServiceClient pool's deadlock safety.

Responsibility: Prove the credential weakref finalizer never self-deadlocks on
    ``_BLOB_SERVICE_POOL_LOCK`` when it fires on a thread that already holds the
    lock (GC during a pooled-dict operation), and that the deferred eviction is
    drained by the next pool operation.
Edit boundaries: Pool lifecycle / deadlock-safety behaviour only. No network or
    real Azure credentials — the credential is a bare object and the pooled
    BlobServiceClient is never used for I/O.
Key entry points: ``test_finalizer_defers_when_pool_lock_held``,
    ``test_pending_eviction_drained_by_next_pool_op``.
Risky contracts: ``_evict_credential_or_defer`` MUST acquire the pool lock
    non-blocking; a blocking acquire reintroduces the CI-hanging self-deadlock.
Validation: ``uv run pytest -q api/tests/test_storage_client_pool.py``.
"""

from __future__ import annotations

import pytest
from api.services.storage import client_pool


class _FakeBlobServiceClient:
    """Stand-in for the real client — accepts any credential, no network."""

    def __init__(self, **_kwargs: object) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _clean_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    # Patch construction so the pool never builds a real (credential-validating,
    # network-capable) client; the deadlock-safety logic is independent of it.
    monkeypatch.setattr(client_pool, "BlobServiceClient", _FakeBlobServiceClient)
    client_pool.reset_blob_service_pool()
    yield
    client_pool.reset_blob_service_pool()


class _Cred:
    """A bare credential object weakref.finalize can attach to."""


@pytest.mark.timeout(15)
def test_finalizer_defers_when_pool_lock_held() -> None:
    """The finalizer must not block when the pool lock is already held.

    Reproduces the CI hang: GC fires a credential finalizer on a thread that is
    mid-construction inside the pool lock. A blocking acquire would self-deadlock
    (pytest-timeout would kill the session). The non-blocking path must record
    the id in the pending set and return immediately.
    """
    cred = _Cred()
    client_pool._blob_service(cred, "elbstg01")
    target_id = id(cred)
    assert target_id in client_pool._BLOB_SERVICE_CREDENTIAL_FINALIZED

    # Simulate the finalizer firing while THIS thread already holds the lock.
    with client_pool._BLOB_SERVICE_POOL_LOCK:
        client_pool._evict_credential_or_defer(target_id)  # must NOT block
        assert target_id in client_pool._PENDING_CRED_EVICTIONS


@pytest.mark.timeout(15)
def test_pending_eviction_drained_by_next_pool_op() -> None:
    """A deferred eviction is drained (clients popped) by the next pool op."""
    cred = _Cred()
    client_pool._blob_service(cred, "elbstg01")
    target_id = id(cred)
    assert any(key[0] == target_id for key in client_pool._BLOB_SERVICE_POOL)

    with client_pool._BLOB_SERVICE_POOL_LOCK:
        client_pool._evict_credential_or_defer(target_id)
    assert target_id in client_pool._PENDING_CRED_EVICTIONS

    # The next pooled lookup drains the pending eviction under the lock.
    other = _Cred()
    client_pool._blob_service(other, "elbstg01")
    assert target_id not in client_pool._PENDING_CRED_EVICTIONS
    assert not any(key[0] == target_id for key in client_pool._BLOB_SERVICE_POOL)


@pytest.mark.timeout(15)
def test_direct_finalizer_eviction_pops_clients_when_lock_free() -> None:
    """When the lock is free, the finalizer evicts the credential's clients."""
    cred = _Cred()
    client_pool._blob_service(cred, "elbstg01")
    target_id = id(cred)
    assert any(key[0] == target_id for key in client_pool._BLOB_SERVICE_POOL)

    # Lock is free here → eviction happens inline, nothing deferred.
    client_pool._evict_credential_or_defer(target_id)
    assert target_id not in client_pool._PENDING_CRED_EVICTIONS
    assert not any(key[0] == target_id for key in client_pool._BLOB_SERVICE_POOL)
