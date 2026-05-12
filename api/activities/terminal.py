"""Activities for the Remote Terminal provisioning orchestrator.

Each activity is single-purpose, idempotent, and side-effect tagged.
Activity inputs are JSON-serialisable dicts (Durable Functions requirement).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from services import compute as compute_svc
from services import keyvault as kv_svc
from services import network as net_svc
from services.azure_clients import credential_for_caller
from services.passwords import generate_admin_password
from services.sanitise import sanitise

LOGGER = logging.getLogger(__name__)

CLOUD_INIT_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "scripts"
    / "cloud-init"
    / "remote-terminal.yaml"
)


def _credential(user_assertion: str | None):
    return credential_for_caller(user_assertion)


def _default_vault_name(subscription_id: str, resource_group: str, vm_name: str) -> str:
    """Compute a globally-unique Key Vault name.

    Key Vault names are unique across the entire Azure cloud, so deriving
    it from the VM name alone collides as soon as two resource groups try
    to provision a terminal with the default name. Suffix with a short
    stable hash of the (sub, RG, VM) tuple. Vault names must be 3..24
    chars, lowercase alphanumeric or '-'.
    """
    import hashlib
    import re

    base = re.sub(r"[^a-z0-9-]", "-", vm_name.lower()).strip("-") or "vm"
    digest = hashlib.sha256(
        f"{subscription_id}|{resource_group}|{vm_name}".lower().encode("utf-8")
    ).hexdigest()[:6]
    # Reserve 4 chars for "kv-" prefix and "-" between base and hash.
    # Total budget = 24, hash = 6, prefix = 3, separator = 1 → base ≤ 14.
    return f"kv-{base[:14].rstrip('-')}-{digest}"[:24]


def activity_ensure_keyvault(payload: dict[str, Any]) -> dict[str, Any]:
    """side-effect: creates Key Vault if missing. Returns vault URI."""
    cred = _credential(payload.get("user_assertion"))
    vault_name = payload.get("vault_name") or _default_vault_name(
        payload["subscription_id"], payload["resource_group"], payload["vm_name"]
    )
    vault_uri = kv_svc.ensure_keyvault(
        cred,
        payload["subscription_id"],
        payload["resource_group"],
        payload["region"],
        vault_name,
        payload.get("tenant_id", os.environ.get("AZURE_TENANT_ID", "")),
        caller_oid=payload.get("owner_oid", ""),
    )
    return {"vault_uri": vault_uri, "vault_name": vault_name}


def activity_ensure_resource_group(payload: dict[str, Any]) -> dict[str, Any]:
    """side-effect: creates RG if missing."""
    cred = _credential(payload.get("user_assertion"))
    net_svc.ensure_resource_group(
        cred,
        payload["subscription_id"],
        payload["resource_group"],
        payload["region"],
    )
    return {"ok": True}


def activity_ensure_network(payload: dict[str, Any]) -> dict[str, Any]:
    """side-effect: creates VNet/Subnet/NSG/Public IP/NIC."""
    cred = _credential(payload.get("user_assertion"))
    info = net_svc.ensure_network(
        cred,
        payload["subscription_id"],
        payload["resource_group"],
        payload["region"],
        payload["vm_name"],
        payload["allowed_ssh_cidr"],
    )
    return {
        "nic_id": info.nic_id,
        "public_ip_id": info.public_ip_id,
        "public_ip_address": info.public_ip_address,
        "fqdn": info.fqdn,
    }


def activity_generate_password(payload: dict[str, Any]) -> dict[str, Any]:
    """side-effect: writes a Key Vault secret named vm-<vm_name>-password."""
    cred = _credential(payload.get("user_assertion"))
    vault_uri = payload.get("vault_uri") or os.environ.get("KEY_VAULT_URI", "")
    if not vault_uri:
        raise ValueError("vault_uri is required — ensure Key Vault was provisioned first")
    password = generate_admin_password(24)
    secret_name = f"vm-{payload['vm_name']}-password"
    secret_id = kv_svc.store_secret(
        cred,
        vault_uri,
        secret_name,
        password,
        tags={
            "vm": payload["vm_name"],
            "rg": payload["resource_group"],
            "owner_oid": payload.get("owner_oid", ""),
        },
    )
    return {"password": password, "secret_uri": secret_id, "secret_name": secret_name}


def activity_create_vm(payload: dict[str, Any]) -> dict[str, Any]:
    """side-effect: creates the Linux VM with cloud-init custom data."""
    cred = _credential(payload.get("user_assertion"))
    cloud_init = CLOUD_INIT_PATH.read_text(encoding="utf-8")
    info = compute_svc.create_terminal_vm(
        cred,
        payload["subscription_id"],
        payload["resource_group"],
        payload["region"],
        payload["vm_name"],
        payload["vm_size"],
        payload["admin_username"],
        payload["admin_password"],
        payload["nic_id"],
        cloud_init,
    )
    return {
        "vm_id": info.vm_id,
        "name": info.name,
        "provisioning_state": info.provisioning_state,
        "principal_id": info.principal_id,
    }


def activity_check_cloud_init(payload: dict[str, Any]) -> dict[str, Any]:
    """side-effect: invokes Run Command on the VM (read-only check).

    Returns {"status": "running" | "done" | "failed" | "unknown"}.
    """
    cred = _credential(payload.get("user_assertion"))
    output = compute_svc.run_shell(
        cred,
        payload["subscription_id"],
        payload["resource_group"],
        payload["vm_name"],
        "test -f /var/lib/cloud/elb-bootstrap.done && echo done || cloud-init status --long",
    )
    text = output.lower()
    if "done" in text and "error" not in text:
        status = "done"
    elif "running" in text:
        status = "running"
    elif "error" in text:
        status = "failed"
    else:
        status = "unknown"
    return {"status": status, "raw": sanitise(output)[:1000]}


def activity_assign_vm_roles(payload: dict[str, Any]) -> dict[str, Any]:
    """side-effect: assigns RBAC roles to the VM's managed identity.

    Grants:
    - Storage Blob Data Contributor on the storage account (azcopy, blob access)
    - AcrPull on the ACR (container image pull)
    - Contributor on the workload RG (elastic-blast needs to manage AKS resources)
    """
    import uuid

    from azure.mgmt.authorization import AuthorizationManagementClient

    cred = _credential(payload.get("user_assertion"))
    sub = payload["subscription_id"]
    principal_id = payload["vm_principal_id"]
    if not principal_id:
        LOGGER.warning("No VM principal_id — skipping role assignments")
        return {"roles_assigned": [], "skipped": True}

    auth_client = AuthorizationManagementClient(cred, sub)
    assigned: list[str] = []

    def _assign(scope: str, role_id: str, label: str) -> None:
        role_def = f"{scope}/providers/Microsoft.Authorization/roleDefinitions/{role_id}"
        name = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{scope}:{principal_id}:{role_id}"))
        try:
            auth_client.role_assignments.create(
                scope, name,
                {
                    "role_definition_id": role_def,
                    "principal_id": principal_id,
                    "principal_type": "ServicePrincipal",
                },
            )
            assigned.append(label)
            LOGGER.info("Assigned %s to VM MI %s on %s", label, principal_id, scope)
        except Exception as exc:
            if "Conflict" in str(exc) or "RoleAssignmentExists" in str(exc):
                assigned.append(f"{label} (exists)")
                LOGGER.debug("Role %s already assigned", label)
            else:
                LOGGER.warning("Failed to assign %s: %s", label, exc)

    # Storage Blob Data Contributor
    storage_rg = payload.get("storage_resource_group") or payload.get("workload_resource_group", "")
    storage_account = payload.get("storage_account", "")
    if storage_rg and storage_account:
        scope = f"/subscriptions/{sub}/resourceGroups/{storage_rg}/providers/Microsoft.Storage/storageAccounts/{storage_account}"
        _assign(scope, "ba92f5b4-2d11-453d-a403-e96b0029c9fe", "StorageBlobDataContributor")

    # AcrPull
    acr_rg = payload.get("acr_resource_group", "")
    acr_name = payload.get("acr_name", "")
    if acr_rg and acr_name:
        scope = f"/subscriptions/{sub}/resourceGroups/{acr_rg}/providers/Microsoft.ContainerRegistry/registries/{acr_name}"
        _assign(scope, "7f951dda-4ed3-4680-a7ca-43fe172d538d", "AcrPull")

    # Contributor on workload RG (elastic-blast manages AKS, storage, etc.)
    workload_rg = payload.get("workload_resource_group", "")
    if workload_rg:
        scope = f"/subscriptions/{sub}/resourceGroups/{workload_rg}"
        _assign(scope, "b24988ac-6180-42a0-ab88-20f7382dd24c", "Contributor")

    return {"roles_assigned": assigned, "principal_id": principal_id}
