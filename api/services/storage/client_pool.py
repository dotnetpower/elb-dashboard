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
_BLOB_SERVICE_THREAD_LOCAL = threading.local()


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
    with _BLOB_SERVICE_POOL_LOCK:
        cached = _BLOB_SERVICE_POOL.get(pool_key)
        if cached is not None:
            cached_client, _last_used = cached
            _BLOB_SERVICE_POOL[pool_key] = (cached_client, now)
            _BLOB_SERVICE_POOL.move_to_end(pool_key)
            thread_cache[pool_key] = cached_client
            return cached_client
        from api.services.storage.endpoint import blob_account_url

        client = BlobServiceClient(
            account_url=blob_account_url(account_name),
            credential=credential,
            retry_total=0,
            connection_timeout=5,
            read_timeout=10,
        )
        _BLOB_SERVICE_POOL[pool_key] = (client, now)
        while len(_BLOB_SERVICE_POOL) > _BLOB_SERVICE_POOL_MAX:
            _evicted_key, (evicted, _ts) = _BLOB_SERVICE_POOL.popitem(last=False)
            evicted_clients.append(evicted)
        _ensure_credential_eviction(credential)
    thread_cache[pool_key] = client
    for evicted in evicted_clients:
        try:
            evicted.close()
        except Exception as exc:
            LOGGER.debug("blob service evict-close failed: %s", type(exc).__name__)
    return client


def _ensure_credential_eviction(credential: Any) -> None:
    """Register a weakref finalizer that evicts pooled clients on GC.

    Must be called from inside ``_BLOB_SERVICE_POOL_LOCK``.
    """
    import weakref

    cred_id = id(credential)
    if cred_id in _BLOB_SERVICE_CREDENTIAL_FINALIZED:
        return

    def _evict_for_credential(target_id: int = cred_id) -> None:
        stale: list[BlobServiceClient] = []
        with _BLOB_SERVICE_POOL_LOCK:
            keys = [key for key in _BLOB_SERVICE_POOL if key[0] == target_id]
            for key in keys:
                client, _ts = _BLOB_SERVICE_POOL.pop(key)
                stale.append(client)
            _BLOB_SERVICE_CREDENTIAL_FINALIZED.discard(target_id)
        for client in stale:
            try:
                client.close()
            except Exception as exc:
                LOGGER.debug(
                    "blob service finalizer close skipped: %s", type(exc).__name__
                )

    try:
        weakref.finalize(credential, _evict_for_credential)
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
    cache = getattr(_BLOB_SERVICE_THREAD_LOCAL, "cache", None)
    if cache is not None:
        cache.clear()
    for client in clients:
        try:
            client.close()
        except Exception as exc:
            LOGGER.debug("blob service reset-close failed: %s", type(exc).__name__)
