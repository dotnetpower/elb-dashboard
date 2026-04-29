"""Helpers for instantiating Azure SDK management clients with a caller credential.

Orchestrator activities pass the caller's bearer token in via input; this
module creates an OBO credential and the typed mgmt clients on demand. We
deliberately do not cache clients per-process because each invocation may
target a different subscription / caller.
"""

from __future__ import annotations

from azure.core.credentials import TokenCredential
from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient
from azure.mgmt.compute import ComputeManagementClient
from azure.mgmt.containerregistry import ContainerRegistryManagementClient
from azure.mgmt.containerservice import ContainerServiceClient
from azure.mgmt.network import NetworkManagementClient
from azure.mgmt.resource import ResourceManagementClient
from azure.mgmt.storage import StorageManagementClient

from auth.obo import caller_credential
from auth.token import DEV_BYPASS_TOKEN


def credential_for_caller(user_assertion: str | None) -> TokenCredential:
    """Return a TokenCredential for the caller.

    When `user_assertion` is a real bearer token we use OBO so downstream
    Azure calls run as the signed-in user. When AUTH_DEV_BYPASS is enabled
    the activity sees a sentinel token and we transparently fall back to
    DefaultAzureCredential (which picks up the developer's local az login).
    """
    if user_assertion and user_assertion != DEV_BYPASS_TOKEN:
        return caller_credential(user_assertion)
    return DefaultAzureCredential(exclude_interactive_browser_credential=True)


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
