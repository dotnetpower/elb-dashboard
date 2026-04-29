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
    vault_uri = os.environ["KEY_VAULT_URI"]
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
