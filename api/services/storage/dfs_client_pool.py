"""Pooled DataLakeServiceClient lifecycle for ADLS Gen2 (dfs) data-plane helpers.

Responsibility: Create, reuse, prune, and reset ``DataLakeServiceClient`` /
``FileSystemClient`` instances per credential/account pair, and expose the
``STORAGE_DFS_ENABLED`` feature gate. This is the dfs sibling of
``client_pool.py`` (which pools ``BlobServiceClient``); both target the same
HNS-enabled storage account.
Edit boundaries: Keep ONLY dfs client pooling, the feature flag, and account-name
validation here. Directory/file I/O (get_paths, delete_directory, rename,
read/stream) belongs in dedicated dfs I/O helpers, not here.
Key entry points: ``dfs_enabled``, ``_dfs_service``, ``_dfs_filesystem``,
``prune_idle_dfs_service_clients``, ``reset_dfs_service_pool``.
Risky contracts: Pool keys include ``id(credential)`` so a client built against
one credential object is never reused for another. The weakref finalizer that
evicts on credential GC is deadlock-safe — it never blocks on the pool lock
(mirrors the proven ``client_pool`` pattern). Account names are validated against
``_STORAGE_ACCOUNT_NAME_RE`` so a forged querystring cannot redirect the MI.
Validation: ``uv run pytest -q api/tests/test_storage_dfs_client_pool.py``.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import weakref
from collections import OrderedDict
from typing import Any

from azure.core.credentials import TokenCredential
from azure.storage.filedatalake import DataLakeServiceClient, FileSystemClient

# Reuse the single source of truth for storage-account-name validation so the
# dfs path enforces exactly the same constraint as the blob path.
from api.services.storage.client_pool import _STORAGE_ACCOUNT_NAME_RE

LOGGER = logging.getLogger(__name__)

_DFS_ENABLED_ENV = "STORAGE_DFS_ENABLED"
_ON_VALUES = {"1", "true", "yes", "on"}

_DFS_SERVICE_POOL_MAX = 32
_DFS_SERVICE_POOL_IDLE_TTL_SECONDS = float(
    os.environ.get("DFS_SERVICE_POOL_IDLE_TTL_SECONDS", "1800.0")
)
_DFS_SERVICE_POOL: OrderedDict[
    tuple[int, str], tuple[DataLakeServiceClient, float]
] = OrderedDict()
_DFS_SERVICE_POOL_LOCK = threading.Lock()
_DFS_SERVICE_CREDENTIAL_FINALIZED: set[int] = set()
# Credential ids whose finalizer fired while the pool lock was already held (GC
# during a pool op). The finalizer MUST NOT block on the lock — that would
# self-deadlock when GC runs on the lock-holding thread — so it records the id
# here and the next pool operation drains it. ``set`` mutation is atomic under
# the GIL, so the finalizer adds without a lock of its own.
_PENDING_CRED_EVICTIONS: set[int] = set()
_DFS_SERVICE_THREAD_LOCAL = threading.local()


def dfs_enabled() -> bool:
    """Return True when the ADLS Gen2 (dfs) data-plane is switched on.

    Default OFF (charter §12a Rule 4: new behaviour ships additive /
    default-OFF). When OFF, every caller must keep using the Blob API path so
    behaviour is byte-for-byte unchanged. Flipping it ON only changes which SDK
    issues the request — both target the same HNS-enabled account — so it is
    safe to toggle without a data migration.
    """
    return os.environ.get(_DFS_ENABLED_ENV, "").strip().lower() in _ON_VALUES


def _close_clients(clients: list[DataLakeServiceClient]) -> None:
    """Close a batch of evicted clients, swallowing per-client errors.

    Always called OUTSIDE ``_DFS_SERVICE_POOL_LOCK`` so a slow ``close()`` never
    holds the pool lock.
    """
    for client in clients:
        try:
            client.close()
        except Exception as exc:
            LOGGER.debug("dfs service close skipped: %s", type(exc).__name__)


def _drain_credential_locked(target_id: int) -> list[DataLakeServiceClient]:
    """Pop every pooled client built against ``target_id``. Caller holds the lock."""
    keys = [key for key in _DFS_SERVICE_POOL if key[0] == target_id]
    stale: list[DataLakeServiceClient] = []
    for key in keys:
        client, _ts = _DFS_SERVICE_POOL.pop(key)
        stale.append(client)
    _DFS_SERVICE_CREDENTIAL_FINALIZED.discard(target_id)
    return stale


def _drain_pending_evictions_locked() -> list[DataLakeServiceClient]:
    """Evict clients for credentials whose finalizer had to defer. Caller holds the lock.

    Only the ids observed at entry are removed, so a finalizer that adds a new id
    mid-drain is picked up on the next call rather than lost.
    """
    if not _PENDING_CRED_EVICTIONS:
        return []
    stale: list[DataLakeServiceClient] = []
    for target_id in list(_PENDING_CRED_EVICTIONS):
        _PENDING_CRED_EVICTIONS.discard(target_id)
        stale.extend(_drain_credential_locked(target_id))
    return stale


def _evict_credential_or_defer(target_id: int) -> None:
    """Weakref-finalizer body: evict pooled clients for a GC'd credential.

    DEADLOCK SAFETY: a credential can be finalized on a thread that ALREADY holds
    ``_DFS_SERVICE_POOL_LOCK`` (GC fires during a pooled-dict operation), so a
    blocking acquire here would self-deadlock the thread. Acquire non-blocking
    instead; if the lock is busy, record the id in ``_PENDING_CRED_EVICTIONS``
    for the next pool operation to drain.
    """
    if not _DFS_SERVICE_POOL_LOCK.acquire(blocking=False):
        _PENDING_CRED_EVICTIONS.add(target_id)
        return
    try:
        stale = _drain_credential_locked(target_id)
    finally:
        _DFS_SERVICE_POOL_LOCK.release()
    _close_clients(stale)


def _ensure_credential_eviction(credential: Any) -> None:
    """Register a weakref finalizer that evicts pooled clients on GC.

    Must be called from inside ``_DFS_SERVICE_POOL_LOCK``. The finalizer body
    (:func:`_evict_credential_or_defer`) is deadlock-safe: it never blocks on the
    pool lock, so it is safe even when GC fires it on a thread already holding it.
    """
    cred_id = id(credential)
    if cred_id in _DFS_SERVICE_CREDENTIAL_FINALIZED:
        return
    try:
        weakref.finalize(credential, _evict_credential_or_defer, cred_id)
    except TypeError:
        return
    _DFS_SERVICE_CREDENTIAL_FINALIZED.add(cred_id)


def _dfs_service(credential: TokenCredential, account_name: str) -> DataLakeServiceClient:
    """Return a pooled ``DataLakeServiceClient`` for ``(credential, account)``.

    Mirrors ``client_pool._blob_service``: a thread-local fast path, a pooled
    slow path under a short-held lock, client construction OUTSIDE the lock, and
    LRU eviction past ``_DFS_SERVICE_POOL_MAX``.
    """
    # Validate the account name so a forged querystring can't redirect the api
    # sidecar's MI to an attacker-controlled URL. Azure storage account names
    # are 3-24 lowercase alphanumeric characters.
    if not _STORAGE_ACCOUNT_NAME_RE.fullmatch(account_name):
        raise ValueError(f"invalid storage account name: {account_name!r}")
    cred_id = id(credential)
    pool_key = (cred_id, account_name)
    thread_cache = getattr(_DFS_SERVICE_THREAD_LOCAL, "cache", None)
    if thread_cache is None:
        thread_cache = {}
        _DFS_SERVICE_THREAD_LOCAL.cache = thread_cache
    cached_local = thread_cache.get(pool_key)
    if cached_local is not None:
        return cached_local
    now = time.monotonic()
    evicted_clients: list[DataLakeServiceClient] = []
    # Fast path: reuse a pooled client. The lock is held only for cheap dict ops
    # — never across client construction — so a GC-triggered finalizer cannot
    # self-deadlock.
    with _DFS_SERVICE_POOL_LOCK:
        evicted_clients.extend(_drain_pending_evictions_locked())
        cached = _DFS_SERVICE_POOL.get(pool_key)
        if cached is not None:
            cached_client, _last_used = cached
            _DFS_SERVICE_POOL[pool_key] = (cached_client, now)
            _DFS_SERVICE_POOL.move_to_end(pool_key)
            thread_cache[pool_key] = cached_client
            _close_clients(evicted_clients)
            return cached_client
    # Build the client OUTSIDE the pool lock. Construction allocates (and may
    # trigger GC, firing a credential finalizer); doing it without the lock held
    # keeps that finalizer's non-blocking acquire from ever contending with us.
    from api.services.storage.endpoint import dfs_account_url

    # read_timeout is 30s (vs the Blob pool's 10s) because dfs callers include
    # directory operations — recursive get_paths over a large job tree — that
    # legitimately take longer than a single blob read. retry_total=0 keeps the
    # api sidecar's own retry/streaming logic authoritative; connection_timeout
    # stays tight so an unreachable dfs private endpoint fails fast.
    client = DataLakeServiceClient(
        account_url=dfs_account_url(account_name),
        credential=credential,
        retry_total=0,
        connection_timeout=5,
        read_timeout=30,
    )
    with _DFS_SERVICE_POOL_LOCK:
        evicted_clients.extend(_drain_pending_evictions_locked())
        # Another thread may have inserted a client for this key while we built
        # ours; prefer the pooled one and discard the redundant build.
        existing = _DFS_SERVICE_POOL.get(pool_key)
        if existing is not None:
            existing_client, _last_used = existing
            _DFS_SERVICE_POOL[pool_key] = (existing_client, now)
            _DFS_SERVICE_POOL.move_to_end(pool_key)
            thread_cache[pool_key] = existing_client
            evicted_clients.append(client)
            _close_clients(evicted_clients)
            return existing_client
        _DFS_SERVICE_POOL[pool_key] = (client, now)
        while len(_DFS_SERVICE_POOL) > _DFS_SERVICE_POOL_MAX:
            _evicted_key, (evicted, _ts) = _DFS_SERVICE_POOL.popitem(last=False)
            evicted_clients.append(evicted)
        _ensure_credential_eviction(credential)
    thread_cache[pool_key] = client
    _close_clients(evicted_clients)
    return client


def _dfs_filesystem(
    credential: TokenCredential, account_name: str, filesystem: str
) -> FileSystemClient:
    """Return a ``FileSystemClient`` for a container (HNS filesystem).

    The ``FileSystemClient`` is a thin view over the pooled service client and
    is cheap to derive per call (it reuses the service client's transport /
    pipeline rather than opening a new connection); the expensive, reusable
    object is the ``DataLakeServiceClient`` held in the pool. ``filesystem`` is
    the container name (``results`` / ``queries`` / ``uploads``).
    """
    service = _dfs_service(credential, account_name)
    return service.get_file_system_client(filesystem)


def prune_idle_dfs_service_clients(*, idle_ttl_seconds: float | None = None) -> int:
    """Evict pooled DataLakeServiceClients that have been idle for too long."""
    ttl = (
        idle_ttl_seconds
        if idle_ttl_seconds is not None
        else _DFS_SERVICE_POOL_IDLE_TTL_SECONDS
    )
    if ttl <= 0:
        return 0
    cutoff = time.monotonic() - ttl
    stale: list[DataLakeServiceClient] = []
    with _DFS_SERVICE_POOL_LOCK:
        keys = [key for key, (_c, ts) in _DFS_SERVICE_POOL.items() if ts < cutoff]
        for key in keys:
            client, _ts = _DFS_SERVICE_POOL.pop(key)
            stale.append(client)
    for client in stale:
        try:
            client.close()
        except Exception as exc:
            LOGGER.debug("dfs service idle-evict close skipped: %s", type(exc).__name__)
    return len(stale)


def reset_dfs_service_pool() -> None:
    """Drop every pooled DataLakeServiceClient (test isolation + shutdown)."""
    with _DFS_SERVICE_POOL_LOCK:
        clients = [client for client, _ts in _DFS_SERVICE_POOL.values()]
        _DFS_SERVICE_POOL.clear()
        _DFS_SERVICE_CREDENTIAL_FINALIZED.clear()
        _PENDING_CRED_EVICTIONS.clear()
    cache = getattr(_DFS_SERVICE_THREAD_LOCAL, "cache", None)
    if cache is not None:
        cache.clear()
    for client in clients:
        try:
            client.close()
        except Exception as exc:
            LOGGER.debug("dfs service reset-close failed: %s", type(exc).__name__)
