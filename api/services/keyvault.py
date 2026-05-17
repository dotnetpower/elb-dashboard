"""Key Vault secret helpers.

Supports two permission models on the existing vault:

* **Access policies** — preferred. Function App MI gets ``set/get/list/delete``
  on secrets via an additive ``update_access_policy`` call (idempotent, never
  re-PUTs the vault).
* **RBAC authorization** — common when subscription Azure Policy forces it on
  every new vault. We do NOT try to flip the permission model (that requires
  ``Microsoft.Authorization/roleAssignments/write`` which the MI lacks under
  Contributor); instead we best-effort grant the MI the
  ``Key Vault Secrets Officer`` role on the vault scope, and log a clear
  manual command when the MI cannot self-grant.
"""

from __future__ import annotations

import base64
import json
import logging
import time

from azure.core.credentials import TokenCredential

from api.services.azure_clients import kv_mgmt_client, kv_secret_client

LOGGER = logging.getLogger(__name__)

# Secret permissions granted to the Function App MI via access policy
_MI_SECRET_PERMISSIONS = ["get", "set", "delete", "list", "purge"]
# Caller only needs to read secrets (e.g. password reveal)
_CALLER_SECRET_PERMISSIONS = ["get", "list"]


def _get_oid_from_credential(credential: TokenCredential) -> str:
    """Extract the object ID from the credential's access token."""
    try:
        token = credential.get_token("https://management.azure.com/.default")
        # JWT payload is the second dot-separated segment, base64url-encoded
        payload_b64 = token.token.split(".")[1]
        payload_b64 += "=" * (4 - len(payload_b64) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload_b64))
        return claims.get("oid", "")
    except Exception as exc:
        LOGGER.warning("Could not extract OID from credential token: %s", exc)
        return ""


def _get_existing_vault(client, resource_group: str, vault_name: str):
    """Return the vault object if it exists, else None."""
    from azure.core.exceptions import HttpResponseError, ResourceNotFoundError

    try:
        return client.vaults.get(resource_group, vault_name)
    except (ResourceNotFoundError, HttpResponseError) as exc:
        if "not found" in str(exc).lower() or "ResourceNotFound" in type(exc).__name__:
            return None
        raise


def _build_access_policies(
    tenant_id: str,
    mi_oid: str,
    caller_oid: str = "",
) -> list[dict]:
    """Build access policy entries for the MI and optionally the caller."""
    policies: list[dict] = []
    if mi_oid:
        policies.append(
            {
                "tenant_id": tenant_id,
                "object_id": mi_oid,
                "permissions": {"secrets": _MI_SECRET_PERMISSIONS},
            }
        )
    if caller_oid and caller_oid != mi_oid:
        policies.append(
            {
                "tenant_id": tenant_id,
                "object_id": caller_oid,
                "permissions": {"secrets": _CALLER_SECRET_PERMISSIONS},
            }
        )
    return policies


def _ensure_vault_config(
    client,
    resource_group: str,
    vault_name: str,
    existing,
    tenant_id: str,
    mi_oid: str,
    caller_oid: str = "",
) -> None:
    """Ensure data-plane access for the MI on an existing vault.

    Two permission models are supported:

    * **RBAC mode** (``enable_rbac_authorization=true``) — common when
      subscription Azure Policy forces it on every new vault. We do NOT
      attempt to flip the permission model: changing it requires
      ``Microsoft.Authorization/roleAssignments/write`` which the Function
      App MI lacks under the standard Contributor baseline, and the request
      fails with ``InsufficientPermissions: Caller is not allowed to change
      permission model``. Instead we leave the model as-is and trust that
      the MI has been granted the ``Key Vault Secrets Officer`` role on the
      vault scope (assigned out-of-band by an admin, or attempted by
      ``_try_assign_secrets_officer`` when the MI happens to have role-
      assignment rights).

    * **Access policy mode** (``enable_rbac_authorization=false``) — the
      original elastic-blast model. We additively add an access policy for
      the MI (and the caller, if provided) using the dedicated
      ``update_access_policy`` operation, which never re-PUTs the vault.
    """
    props = existing.properties

    if getattr(props, "enable_rbac_authorization", False):
        LOGGER.info(
            "Vault %s uses RBAC authorization — skipping permission-model PATCH; "
            "MI %s must hold 'Key Vault Secrets Officer' on the vault.",
            vault_name,
            mi_oid,
        )
        return

    if getattr(props, "public_network_access", "Enabled") != "Enabled":
        LOGGER.info("Re-enabling public network access on %s", vault_name)
        client.vaults.update(
            resource_group,
            vault_name,
            {"properties": {"public_network_access": "Enabled"}},
        )

    existing_oids = {p.object_id for p in (getattr(props, "access_policies", None) or [])}
    policies_to_add = [
        p
        for p in _build_access_policies(tenant_id, mi_oid, caller_oid)
        if p["object_id"] not in existing_oids
    ]
    if policies_to_add:
        LOGGER.info("Adding %d access policies to %s", len(policies_to_add), vault_name)
        client.vaults.update_access_policy(
            resource_group,
            vault_name,
            "add",
            {"properties": {"access_policies": policies_to_add}},
        )


def _try_assign_secrets_officer(
    credential: TokenCredential,
    subscription_id: str,
    vault_id: str,
    mi_oid: str,
    caller_oid: str = "",
) -> None:
    """Best-effort: grant 'Key Vault Secrets Officer' to MI / caller.

    Used after a vault is created (or detected) in RBAC mode. The Function
    App MI usually has only Contributor on the subscription, which is
    insufficient for ``roleAssignments/write``; in that case we log the
    exact ``az role assignment create`` command an admin can run to
    unblock provisioning.
    """
    import uuid as _uuid

    from azure.mgmt.authorization import AuthorizationManagementClient

    role_id = "b86a8fe4-44ce-4948-aee5-eccb2c155cd7"  # Key Vault Secrets Officer
    role_def = (
        f"/subscriptions/{subscription_id}/providers/Microsoft.Authorization"
        f"/roleDefinitions/{role_id}"
    )
    auth_client = AuthorizationManagementClient(credential, subscription_id)

    def _assign(principal_id: str, principal_type: str, label: str) -> None:
        if not principal_id:
            return
        name = str(_uuid.uuid5(_uuid.NAMESPACE_URL, f"{vault_id}/{principal_id}/secrets-officer"))
        try:
            auth_client.role_assignments.create(
                scope=vault_id,
                role_assignment_name=name,
                parameters={
                    "properties": {
                        "role_definition_id": role_def,
                        "principal_id": principal_id,
                        "principal_type": principal_type,
                    }
                },
            )
            LOGGER.info(
                "Assigned 'Key Vault Secrets Officer' to %s %s on %s", label, principal_id, vault_id
            )
            time.sleep(15)  # RBAC propagation
        except Exception as exc:
            msg = str(exc)
            if "RoleAssignmentExists" in msg or "Conflict" in msg:
                LOGGER.debug("Role already assigned for %s on %s", label, vault_id)
                return
            if (
                "AuthorizationFailed" in msg
                or "InsufficientPermissions" in msg
                or "does not have authorization" in msg
            ):
                LOGGER.warning(
                    "Cannot self-grant 'Key Vault Secrets Officer' to %s on %s. "
                    "Run as admin: az role assignment create "
                    "--assignee-object-id %s --assignee-principal-type %s "
                    "--role 'Key Vault Secrets Officer' --scope '%s'",
                    label,
                    vault_id,
                    principal_id,
                    principal_type,
                    vault_id,
                )
                return
            LOGGER.warning("Role assignment for %s failed: %s", label, msg[:200])

    _assign(mi_oid, "ServicePrincipal", "MI")
    if caller_oid and caller_oid != mi_oid:
        _assign(caller_oid, "User", "caller")


def ensure_keyvault(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    region: str,
    vault_name: str,
    tenant_id: str,
    caller_oid: str = "",
) -> str:
    """Create a Key Vault if it doesn't exist. Returns the vault URI.

    Uses access policies (not RBAC) so that the Function App's Managed Identity
    can manage secrets with just Contributor role on the subscription.

    side-effect: creates Key Vault + access policies. Idempotent.
    """
    client = kv_mgmt_client(credential, subscription_id)
    mi_oid = _get_oid_from_credential(credential)
    LOGGER.info("ensure_keyvault name=%s rg=%s mi_oid=%s", vault_name, resource_group, mi_oid)

    existing = _get_existing_vault(client, resource_group, vault_name)
    if existing:
        LOGGER.info("Key Vault %s already exists", vault_name)
        _ensure_vault_config(
            client, resource_group, vault_name, existing, tenant_id, mi_oid, caller_oid
        )
        if getattr(existing.properties, "enable_rbac_authorization", False):
            vault_id = (
                f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
                f"/providers/Microsoft.KeyVault/vaults/{vault_name}"
            )
            _try_assign_secrets_officer(credential, subscription_id, vault_id, mi_oid, caller_oid)
        return existing.properties.vault_uri

    access_policies = _build_access_policies(tenant_id, mi_oid, caller_oid)
    poller = client.vaults.begin_create_or_update(
        resource_group,
        vault_name,
        {
            "location": region,
            "properties": {
                "sku": {"family": "A", "name": "standard"},
                "tenant_id": tenant_id,
                "access_policies": access_policies,
                "enable_rbac_authorization": False,
                "enable_soft_delete": True,
                "soft_delete_retention_in_days": 7,
                "public_network_access": "Enabled",
            },
            "tags": {"managed-by": "elb-dashboard"},
        },
    )
    vault = poller.result()
    vault_uri = vault.properties.vault_uri
    LOGGER.info("Key Vault %s created: %s", vault_name, vault_uri)

    # Subscription Azure Policy may force enable_rbac_authorization=true on
    # every new vault regardless of what we asked for in the create body.
    # When that happens, fall back to RBAC role assignment so the data plane
    # is still reachable.
    if getattr(vault.properties, "enable_rbac_authorization", False):
        LOGGER.info(
            "Newly created vault %s ended up in RBAC mode (subscription policy). "
            "Granting 'Key Vault Secrets Officer' to MI.",
            vault_name,
        )
        _try_assign_secrets_officer(credential, subscription_id, vault.id, mi_oid, caller_oid)

    return vault_uri


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
