"""Azure SDK client factories for service wrappers.

Responsibility: Azure SDK client factories for service wrappers
Edit boundaries: Keep reusable domain logic here; routes and tasks should call this layer
instead of duplicating SDK code.
Key entry points: `_get_mi_credential`, `credential_for_caller`, `resource_client`,
`network_client`, `compute_client`, `storage_client`, `subscription_client`,
`authorization_client`, `msi_client`, `reset_mgmt_client_pool`
Risky contracts: Use managed identity/DefaultAzureCredential only; do not add client secrets or
OBO flows. Pooled ARM clients are reused across threads/requests keyed by
`(kind, id(credential), subscription_id)`; tests reset the pool via the autouse
`_reset_mgmt_client_pool` fixture in `conftest.py`.
Validation: `uv run pytest -q api/tests`.
"""

from __future__ import annotations

import logging
import os
import threading
import weakref
from collections.abc import Callable
from typing import Any

from azure.core.credentials import TokenCredential
from azure.keyvault.secrets import SecretClient
from azure.mgmt.compute import ComputeManagementClient
from azure.mgmt.containerregistry import ContainerRegistryManagementClient
from azure.mgmt.containerservice import ContainerServiceClient
from azure.mgmt.keyvault import KeyVaultManagementClient
from azure.mgmt.network import NetworkManagementClient
from azure.mgmt.resource import ResourceManagementClient
from azure.mgmt.storage import StorageManagementClient

LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Management-client pool.
#
# Each ``ResourceManagementClient(credential, sub)`` (and the other ARM
# clients) builds its own HTTP pipeline with a private connection pool. On a
# polling dashboard the monitor routes reconstruct these on every cache miss,
# forcing a fresh TLS handshake to ``management.azure.com`` each time. The
# Azure track-2 SDK clients are safe to reuse across threads, so we pool one
# instance per (client kind, credential identity, subscription) and reuse the
# warm connection pool. Keyed by ``id(credential)`` like the BlobServiceClient
# pool; a weakref finalizer evicts a credential's clients when it is GC'd so a
# rotated credential never lingers. ``ENABLE_MGMT_CLIENT_POOL=false`` restores
# the legacy construct-per-call behaviour.
# ---------------------------------------------------------------------------
_MGMT_CLIENT_POOL: dict[tuple[str, int, str], Any] = {}
_MGMT_CLIENT_POOL_LOCK = threading.Lock()
_MGMT_CLIENT_FINALIZED: set[int] = set()


def _mgmt_pool_enabled() -> bool:
    return os.environ.get("ENABLE_MGMT_CLIENT_POOL", "true").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _register_mgmt_credential_eviction(credential: Any) -> None:
    """Evict a credential's pooled clients when the credential is GC'd.

    Must be called while holding ``_MGMT_CLIENT_POOL_LOCK``.
    """
    cred_id = id(credential)
    if cred_id in _MGMT_CLIENT_FINALIZED:
        return

    def _evict(target_id: int = cred_id) -> None:
        stale: list[Any] = []
        with _MGMT_CLIENT_POOL_LOCK:
            for key in [k for k in _MGMT_CLIENT_POOL if k[1] == target_id]:
                stale.append(_MGMT_CLIENT_POOL.pop(key))
            _MGMT_CLIENT_FINALIZED.discard(target_id)
        for client in stale:
            _close_quietly(client)

    try:
        weakref.finalize(credential, _evict)
    except TypeError:
        return
    _MGMT_CLIENT_FINALIZED.add(cred_id)


def _close_quietly(client: Any) -> None:
    close = getattr(client, "close", None)
    if not callable(close):
        return
    try:
        close()
    except Exception as exc:
        LOGGER.debug("mgmt client close skipped: %s", type(exc).__name__)


def _pooled_mgmt_client[T](
    kind: str,
    credential: TokenCredential,
    subscription_id: str,
    factory: Callable[[], T],
) -> T:
    """Return a pooled ARM client for ``(kind, credential, subscription_id)``.

    ``factory`` is only invoked on a cache miss, so monkeypatched SDK classes
    are still honoured the first time a given key is built.
    """
    if not _mgmt_pool_enabled():
        return factory()
    key = (kind, id(credential), subscription_id)
    cached = _MGMT_CLIENT_POOL.get(key)
    if cached is not None:
        return cached
    with _MGMT_CLIENT_POOL_LOCK:
        cached = _MGMT_CLIENT_POOL.get(key)
        if cached is not None:
            return cached
        client = factory()
        _MGMT_CLIENT_POOL[key] = client
        _register_mgmt_credential_eviction(credential)
        return client


def reset_mgmt_client_pool() -> None:
    """Drop every pooled management client (test hook + credential rotation)."""
    with _MGMT_CLIENT_POOL_LOCK:
        clients = list(_MGMT_CLIENT_POOL.values())
        _MGMT_CLIENT_POOL.clear()
        _MGMT_CLIENT_FINALIZED.clear()
    for client in clients:
        _close_quietly(client)


def _get_mi_credential() -> TokenCredential:
    """Return the shared Azure credential (Managed Identity in Azure, az login locally)."""
    from api.services import get_credential

    return get_credential()


def credential_for_caller(user_assertion: str | None = None) -> TokenCredential:
    """Return a TokenCredential for Azure operations.

    Always uses Managed Identity (DefaultAzureCredential). The user_assertion
    parameter is accepted for API compatibility but not used for OBO.
    User authorization is handled by JWT validation in token.py.
    """
    return _get_mi_credential()


# Alias for activities that receive assertion from orchestrator input
credential_for_assertion = credential_for_caller


def resource_client(credential: TokenCredential, subscription_id: str) -> ResourceManagementClient:
    return _pooled_mgmt_client(
        "resource",
        credential,
        subscription_id,
        lambda: ResourceManagementClient(credential, subscription_id),
    )


def network_client(credential: TokenCredential, subscription_id: str) -> NetworkManagementClient:
    return _pooled_mgmt_client(
        "network",
        credential,
        subscription_id,
        lambda: NetworkManagementClient(credential, subscription_id),
    )


def compute_client(credential: TokenCredential, subscription_id: str) -> ComputeManagementClient:
    return _pooled_mgmt_client(
        "compute",
        credential,
        subscription_id,
        lambda: ComputeManagementClient(credential, subscription_id),
    )


def storage_client(credential: TokenCredential, subscription_id: str) -> StorageManagementClient:
    return _pooled_mgmt_client(
        "storage",
        credential,
        subscription_id,
        lambda: StorageManagementClient(credential, subscription_id),
    )


def acr_client(
    credential: TokenCredential, subscription_id: str
) -> ContainerRegistryManagementClient:
    return _pooled_mgmt_client(
        "acr",
        credential,
        subscription_id,
        lambda: ContainerRegistryManagementClient(credential, subscription_id),
    )


def aks_client(credential: TokenCredential, subscription_id: str) -> ContainerServiceClient:
    return _pooled_mgmt_client(
        "aks",
        credential,
        subscription_id,
        lambda: ContainerServiceClient(credential, subscription_id),
    )


def kv_secret_client(credential: TokenCredential, vault_uri: str) -> SecretClient:
    return SecretClient(vault_url=vault_uri, credential=credential)


def kv_mgmt_client(credential: TokenCredential, subscription_id: str) -> KeyVaultManagementClient:
    return _pooled_mgmt_client(
        "kv_mgmt",
        credential,
        subscription_id,
        lambda: KeyVaultManagementClient(credential, subscription_id),
    )


# ---------------------------------------------------------------------------
# Lazy-import factories.
#
# These SDK clients are imported inside the function (not at module top) on
# purpose: several tests monkeypatch the class on its azure.mgmt.* module
# (e.g. ``monkeypatch.setattr("azure.mgmt.resource.SubscriptionClient", Fake)``)
# and rely on the construction resolving the patched class at call time. A
# top-level import would bind the real class once at import and defeat those
# patches. Keep the import inside the function.
# ---------------------------------------------------------------------------


def subscription_client(credential: TokenCredential) -> Any:
    """Return a SubscriptionClient (tenant-wide subscription listing)."""
    from azure.mgmt.resource import SubscriptionClient

    return SubscriptionClient(credential)


def authorization_client(credential: TokenCredential, subscription_id: str) -> Any:
    """Return an AuthorizationManagementClient (RBAC role assignments)."""
    from azure.mgmt.authorization import AuthorizationManagementClient

    return AuthorizationManagementClient(credential, subscription_id)


def msi_client(credential: TokenCredential, subscription_id: str) -> Any:
    """Return a ManagedServiceIdentityClient (user-assigned MI + federated creds)."""
    from azure.mgmt.msi import ManagedServiceIdentityClient

    return ManagedServiceIdentityClient(credential, subscription_id)

