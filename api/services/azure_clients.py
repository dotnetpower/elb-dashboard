"""Azure SDK client factories for service wrappers.

Responsibility: Azure SDK client factories for service wrappers
Edit boundaries: Keep reusable domain logic here; routes and tasks should call this layer
instead of duplicating SDK code.
Key entry points: `_get_mi_credential`, `credential_for_caller`, `resource_client`,
`network_client`, `compute_client`, `storage_client`
Risky contracts: Use managed identity/DefaultAzureCredential only; do not add client secrets or
OBO flows.
Validation: `uv run pytest -q api/tests`.
"""

from __future__ import annotations

import logging

from azure.core.credentials import TokenCredential
from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient
from azure.mgmt.compute import ComputeManagementClient
from azure.mgmt.containerregistry import ContainerRegistryManagementClient
from azure.mgmt.containerservice import ContainerServiceClient
from azure.mgmt.keyvault import KeyVaultManagementClient
from azure.mgmt.network import NetworkManagementClient
from azure.mgmt.resource import ResourceManagementClient
from azure.mgmt.storage import StorageManagementClient

LOGGER = logging.getLogger(__name__)

# Singleton MI credential — reused across all calls (safe, thread-safe)
_MI_CREDENTIAL: DefaultAzureCredential | None = None


def _get_mi_credential() -> DefaultAzureCredential:
    """Return a cached DefaultAzureCredential (Managed Identity in Azure, az login locally)."""
    global _MI_CREDENTIAL
    if _MI_CREDENTIAL is None:
        _MI_CREDENTIAL = DefaultAzureCredential(exclude_interactive_browser_credential=True)
    return _MI_CREDENTIAL


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
