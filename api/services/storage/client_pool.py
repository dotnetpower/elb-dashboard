"""Pooled BlobServiceClient lifecycle for Storage data-plane helpers.

Responsibility: Create, reuse, prune, and reset BlobServiceClient instances per
credential/account pair.
Edit boundaries: Keep only BlobServiceClient pooling and account-name validation
here. Blob upload/download/listing logic stays in `data.py`.
Key entry points: `_blob_service`, `prune_idle_blob_service_clients`,
`reset_blob_service_pool`.
Risky contracts: Pool keys include `id(credential)` so a client built against one
credential object is never reused for another. Finalizers evict clients when a
credential is garbage-collected.
Validation: `uv run pytest -q api/tests/test_storage_data.py`.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections import OrderedDict
from typing import Any

from azure.core.credentials import TokenCredential
from azure.storage.blob import BlobServiceClient

LOGGER = logging.getLogger(__name__)

_STORAGE_ACCOUNT_NAME_RE = re.compile(r"^[a-z0-9]{3,24}$")
_BLOB_SERVICE_POOL_MAX = 32
_BLOB_SERVICE_POOL_IDLE_TTL_SECONDS = float(
    os.environ.get("BLOB_SERVICE_POOL_IDLE_TTL_SECONDS", "1800.0")
)
_BLOB_SERVICE_POOL: OrderedDict[
    tuple[int, str], tuple[BlobServiceClient, float]
] = OrderedDict()
_BLOB_SERVICE_POOL_LOCK = threading.Lock()
_BLOB_SERVICE_CREDENTIAL_FINALIZED: set[int] = set()
# Credential ids whose weakref finalizer fired while ``_BLOB_SERVICE_POOL_LOCK``
# was already held (the GC-during-pool-op case). The finalizer MUST NOT block on
# the lock — doing so self-deadlocks when GC runs on the same thread that holds
# it — so it records the id here and the next pool operation drains it under the
# lock. ``set`` mutation is atomic under the GIL, so the finalizer adds without a
# lock of its own.
_PENDING_CRED_EVICTIONS: set[int] = set()
_BLOB_SERVICE_THREAD_LOCAL = threading.local()


def _close_clients(clients: list[BlobServiceClient]) -> None:
    """Close a batch of evicted clients, swallowing per-client errors.

    Always called OUTSIDE ``_BLOB_SERVICE_POOL_LOCK`` so a slow ``close()`` never
    holds the pool lock.
    """
    for client in clients:
        try:
            client.close()
        except Exception as exc:
            LOGGER.debug("blob service close skipped: %s", type(exc).__name__)


def _drain_credential_locked(target_id: int) -> list[BlobServiceClient]:
    """Pop every pooled client built against ``target_id``. Caller holds the lock."""
    keys = [key for key in _BLOB_SERVICE_POOL if key[0] == target_id]
    stale: list[BlobServiceClient] = []
    for key in keys:
        client, _ts = _BLOB_SERVICE_POOL.pop(key)
        stale.append(client)
    _BLOB_SERVICE_CREDENTIAL_FINALIZED.discard(target_id)
    return stale


def _drain_pending_evictions_locked() -> list[BlobServiceClient]:
    """Evict clients for credentials whose finalizer had to defer. Caller holds the lock.

    Only the ids observed at entry are removed, so a finalizer that adds a new id
    mid-drain is picked up on the next call rather than lost.
    """
    if not _PENDING_CRED_EVICTIONS:
        return []
    stale: list[BlobServiceClient] = []
    for target_id in list(_PENDING_CRED_EVICTIONS):
        _PENDING_CRED_EVICTIONS.discard(target_id)
        stale.extend(_drain_credential_locked(target_id))
    return stale


def _evict_credential_or_defer(target_id: int) -> None:
    """Weakref-finalizer body: evict pooled clients for a GC'd credential.

    DEADLOCK SAFETY: a credential can be finalized on a thread that ALREADY holds
    ``_BLOB_SERVICE_POOL_LOCK`` (GC fires during a pooled-dict operation), so a
    blocking ``with _BLOB_SERVICE_POOL_LOCK`` here would self-deadlock the thread.
    Acquire non-blocking instead; if the lock is busy, record the id in
    ``_PENDING_CRED_EVICTIONS`` for the next pool operation to drain.
    """
    if not _BLOB_SERVICE_POOL_LOCK.acquire(blocking=False):
        _PENDING_CRED_EVICTIONS.add(target_id)
        return
    try:
        stale = _drain_credential_locked(target_id)
    finally:
        _BLOB_SERVICE_POOL_LOCK.release()
    _close_clients(stale)


def _blob_service(credential: TokenCredential, account_name: str) -> BlobServiceClient:
    # Validate the account name so a forged querystring can't redirect the
    # api sidecar's MI to an attacker-controlled URL. Azure storage account
    # names are 3-24 lowercase alphanumeric characters.
    if not _STORAGE_ACCOUNT_NAME_RE.fullmatch(account_name):
        raise ValueError(f"invalid storage account name: {account_name!r}")
    cred_id = id(credential)
    pool_key = (cred_id, account_name)
    thread_cache = getattr(_BLOB_SERVICE_THREAD_LOCAL, "cache", None)
    if thread_cache is None:
        thread_cache = {}
        _BLOB_SERVICE_THREAD_LOCAL.cache = thread_cache
    cached_local = thread_cache.get(pool_key)
    if cached_local is not None:
        return cached_local
    now = time.monotonic()
    evicted_clients: list[BlobServiceClient] = []
    # Fast path: reuse a pooled client if one already exists. The lock is held
    # only for cheap dict ops here — never across the BlobServiceClient
    # construction below — so a GC-triggered finalizer cannot self-deadlock.
    with _BLOB_SERVICE_POOL_LOCK:
        evicted_clients.extend(_drain_pending_evictions_locked())
        cached = _BLOB_SERVICE_POOL.get(pool_key)
        if cached is not None:
            cached_client, _last_used = cached
            _BLOB_SERVICE_POOL[pool_key] = (cached_client, now)
            _BLOB_SERVICE_POOL.move_to_end(pool_key)
            thread_cache[pool_key] = cached_client
            _close_clients(evicted_clients)
            return cached_client
    # Build the client OUTSIDE the pool lock. Construction allocates (and may
    # trigger GC, firing a credential finalizer); doing it without the lock held
    # keeps that finalizer's non-blocking acquire from ever contending with us.
    from api.services.storage.endpoint import blob_account_url

    client = BlobServiceClient(
        account_url=blob_account_url(account_name),
        credential=credential,
        retry_total=0,
        connection_timeout=5,
        read_timeout=10,
    )
    with _BLOB_SERVICE_POOL_LOCK:
        evicted_clients.extend(_drain_pending_evictions_locked())
        # Another thread may have inserted a client for this key while we built
        # ours; prefer the pooled one and discard the redundant build.
        existing = _BLOB_SERVICE_POOL.get(pool_key)
        if existing is not None:
            existing_client, _last_used = existing
            _BLOB_SERVICE_POOL[pool_key] = (existing_client, now)
            _BLOB_SERVICE_POOL.move_to_end(pool_key)
            thread_cache[pool_key] = existing_client
            evicted_clients.append(client)
            _close_clients(evicted_clients)
            return existing_client
        _BLOB_SERVICE_POOL[pool_key] = (client, now)
        while len(_BLOB_SERVICE_POOL) > _BLOB_SERVICE_POOL_MAX:
            _evicted_key, (evicted, _ts) = _BLOB_SERVICE_POOL.popitem(last=False)
            evicted_clients.append(evicted)
        _ensure_credential_eviction(credential)
    thread_cache[pool_key] = client
    _close_clients(evicted_clients)
    return client


def _ensure_credential_eviction(credential: Any) -> None:
    """Register a weakref finalizer that evicts pooled clients on GC.

    Must be called from inside ``_BLOB_SERVICE_POOL_LOCK``. The finalizer body
    (:func:`_evict_credential_or_defer`) is deadlock-safe: it never blocks on the
    pool lock, so it is safe even when GC fires it on a thread already holding it.
    """
    import weakref

    cred_id = id(credential)
    if cred_id in _BLOB_SERVICE_CREDENTIAL_FINALIZED:
        return
    try:
        weakref.finalize(credential, _evict_credential_or_defer, cred_id)
    except TypeError:
        return
    _BLOB_SERVICE_CREDENTIAL_FINALIZED.add(cred_id)


def prune_idle_blob_service_clients(
    *, idle_ttl_seconds: float | None = None
) -> int:
    """Evict pooled BlobServiceClients that have been idle for too long."""
    ttl = (
        idle_ttl_seconds
        if idle_ttl_seconds is not None
        else _BLOB_SERVICE_POOL_IDLE_TTL_SECONDS
    )
    if ttl <= 0:
        return 0
    cutoff = time.monotonic() - ttl
    stale: list[BlobServiceClient] = []
    with _BLOB_SERVICE_POOL_LOCK:
        keys = [key for key, (_c, ts) in _BLOB_SERVICE_POOL.items() if ts < cutoff]
        for key in keys:
            client, _ts = _BLOB_SERVICE_POOL.pop(key)
            stale.append(client)
    for client in stale:
        try:
            client.close()
        except Exception as exc:
            LOGGER.debug("blob service idle-evict close skipped: %s", type(exc).__name__)
    return len(stale)


def reset_blob_service_pool() -> None:
    """Drop every pooled BlobServiceClient."""
    with _BLOB_SERVICE_POOL_LOCK:
        clients = [client for client, _ts in _BLOB_SERVICE_POOL.values()]
        _BLOB_SERVICE_POOL.clear()
        _BLOB_SERVICE_CREDENTIAL_FINALIZED.clear()
        _PENDING_CRED_EVICTIONS.clear()
    cache = getattr(_BLOB_SERVICE_THREAD_LOCAL, "cache", None)
    if cache is not None:
        cache.clear()
    for client in clients:
        try:
            client.close()
        except Exception as exc:
            LOGGER.debug("blob service reset-close failed: %s", type(exc).__name__)
