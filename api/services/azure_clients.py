"""Azure SDK client factories for service wrappers.

Responsibility: Azure SDK client factories for service wrappers
Edit boundaries: Keep reusable domain logic here; routes and tasks should call this layer
instead of duplicating SDK code.
Key entry points: `_get_mi_credential`, `credential_for_caller`, `resource_client`,
`network_client`, `compute_client`, `storage_client`, `subscription_client`,
`authorization_client`, `msi_client`
Risky contracts: Use managed identity/DefaultAzureCredential only; do not add client secrets or
OBO flows.
Validation: `uv run pytest -q api/tests`.
"""

from __future__ import annotations

import logging
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
    return ResourceManagementClient(credential, subscription_id)


def network_client(credential: TokenCredential, subscription_id: str) -> NetworkManagementClient:
    return NetworkManagementClient(credential, subscription_id)


def compute_client(credential: TokenCredential, subscription_id: str) -> ComputeManagementClient:
    return ComputeManagementClient(credential, subscription_id)


def storage_client(credential: TokenCredential, subscription_id: str) -> StorageManagementClient:
    return StorageManagementClient(credential, subscription_id)


def acr_client(
    credential: TokenCredential, subscription_id: str
) -> ContainerRegistryManagementClient:
    return ContainerRegistryManagementClient(credential, subscription_id)


def aks_client(credential: TokenCredential, subscription_id: str) -> ContainerServiceClient:
    return ContainerServiceClient(credential, subscription_id)


def kv_secret_client(credential: TokenCredential, vault_uri: str) -> SecretClient:
    return SecretClient(vault_url=vault_uri, credential=credential)


def kv_mgmt_client(credential: TokenCredential, subscription_id: str) -> KeyVaultManagementClient:
    return KeyVaultManagementClient(credential, subscription_id)


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

