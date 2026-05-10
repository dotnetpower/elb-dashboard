"""Key Vault secret helpers."""

from __future__ import annotations

import logging
import time
import uuid

from azure.core.credentials import TokenCredential

from services.azure_clients import kv_secret_client, kv_mgmt_client

LOGGER = logging.getLogger(__name__)

# Key Vault Secrets Officer built-in role ID
_KV_SECRETS_OFFICER_ROLE = "b86a8fe4-44ce-4948-aee5-eccb2c155cd7"


def _get_existing_vault(client, resource_group: str, vault_name: str):
    """Return the vault object if it exists, else None."""
    from azure.core.exceptions import ResourceNotFoundError, HttpResponseError

    try:
        return client.vaults.get(resource_group, vault_name)
    except (ResourceNotFoundError, HttpResponseError) as exc:
        if "not found" in str(exc).lower() or "ResourceNotFound" in type(exc).__name__:
            return None
        raise


def _ensure_vault_config(client, resource_group: str, vault_name: str, existing, tenant_id: str) -> None:
    """Ensure public network access is enabled and RBAC authorization is on."""
    props = existing.properties
    needs_update = (
        getattr(props, "public_network_access", "Enabled") != "Enabled"
        or not getattr(props, "enable_rbac_authorization", False)
    )
    if not needs_update:
        return
    LOGGER.info("Updating vault config on %s (public_network_access/RBAC)", vault_name)
    client.vaults.begin_create_or_update(
        resource_group,
        vault_name,
        {
            "location": existing.location,
            "properties": {
                "sku": {"family": "A", "name": "standard"},
                "tenant_id": tenant_id,
                "enable_rbac_authorization": True,
                "public_network_access": "Enabled",
            },
        },
    ).result()


def ensure_keyvault(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    region: str,
    vault_name: str,
    tenant_id: str,
    caller_oid: str = "",
) -> str:
    """Create a Key Vault if it doesn't exist and assign RBAC. Returns the vault URI.

    side-effect: creates Key Vault + role assignment. Idempotent.
    """
    client = kv_mgmt_client(credential, subscription_id)
    LOGGER.info("ensure_keyvault name=%s rg=%s", vault_name, resource_group)
    existing = _get_existing_vault(client, resource_group, vault_name)
    if existing:
        LOGGER.info("Key Vault %s already exists", vault_name)
        # Always ensure public network access + RBAC auth
        _ensure_vault_config(client, resource_group, vault_name, existing, tenant_id)
        if caller_oid:
            _assign_kv_role(credential, subscription_id, existing.id, caller_oid, tenant_id)
        return existing.properties.vault_uri

    poller = client.vaults.begin_create_or_update(
        resource_group,
        vault_name,
        {
            "location": region,
            "properties": {
                "sku": {"family": "A", "name": "standard"},
                "tenant_id": tenant_id,
                "access_policies": [],
                "enable_rbac_authorization": True,
                "enable_soft_delete": True,
                "soft_delete_retention_in_days": 7,
                "public_network_access": "Enabled",
            },
            "tags": {"managed-by": "elastic-blast-azure-functionapp"},
        },
    )
    vault = poller.result()
    vault_uri = vault.properties.vault_uri
    vault_id = vault.id
    LOGGER.info("Key Vault %s created: %s", vault_name, vault_uri)

    # Assign Key Vault Secrets Officer role to the caller
    if caller_oid:
        _assign_kv_role(credential, subscription_id, vault_id, caller_oid, tenant_id)

    return vault_uri


def _assign_kv_role(
    credential: TokenCredential,
    subscription_id: str,
    vault_id: str,
    principal_id: str,
    tenant_id: str,
) -> None:
    """Assign Key Vault Secrets Officer role to a principal on the vault."""
    from azure.mgmt.authorization import AuthorizationManagementClient

    auth_client = AuthorizationManagementClient(credential, subscription_id)
    role_definition_id = f"{vault_id}/providers/Microsoft.Authorization/roleDefinitions/{_KV_SECRETS_OFFICER_ROLE}"
    assignment_name = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{vault_id}/{principal_id}/secrets-officer"))

    try:
        auth_client.role_assignments.create(
            scope=vault_id,
            role_assignment_name=assignment_name,
            parameters={
                "properties": {
                    "role_definition_id": role_definition_id,
                    "principal_id": principal_id,
                    "principal_type": "User",
                }
            },
        )
        LOGGER.info("Assigned KV Secrets Officer to %s on %s", principal_id, vault_id)
        # RBAC propagation delay
        time.sleep(15)
    except Exception as exc:
        if "RoleAssignmentExists" in str(exc) or "Conflict" in str(exc):
            LOGGER.info("Role assignment already exists for %s", principal_id)
        else:
            LOGGER.warning("Failed to assign KV role: %s", exc)


def store_secret(
    credential: TokenCredential,
    vault_uri: str,
    name: str,
    value: str,
    tags: dict[str, str] | None = None,
) -> str:
    """Set or update a secret. Returns the secret's full id (including version)."""
    LOGGER.info("store_secret name=%s vault=%s", name, vault_uri)
    client = kv_secret_client(credential, vault_uri)
    secret = client.set_secret(name, value, tags=tags)
    return secret.id or ""


def get_secret(credential: TokenCredential, vault_uri: str, name: str) -> str:
    """Read the latest version of a secret."""
    LOGGER.info("get_secret name=%s vault=%s", name, vault_uri)
    client = kv_secret_client(credential, vault_uri)
    return client.get_secret(name).value or ""


def delete_secret(credential: TokenCredential, vault_uri: str, name: str) -> None:
    LOGGER.info("delete_secret name=%s vault=%s", name, vault_uri)
    client = kv_secret_client(credential, vault_uri)
    client.begin_delete_secret(name).wait()
