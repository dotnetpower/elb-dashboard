"""Provision Terminal orchestrator.

Sequence (each step is an idempotent activity):
  1. ensure_resource_group
  2. ensure_network              -> NIC, public IP, FQDN
  3. generate_admin_password     -> Key Vault secret
  4. create_vm (custom_data = cloud-init)
  5. wait for cloud-init to finish (poll up to N minutes)

Output: TerminalConnectionInfo (FQDN, username, password secret URI, status).
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import azure.durable_functions as df

LOGGER = logging.getLogger(__name__)

CLOUD_INIT_POLL_INTERVAL_SECONDS = 30
CLOUD_INIT_MAX_ATTEMPTS = 30  # 30 * 30s = 15 min hard ceiling


def provision_terminal_orchestrator(
    context: df.DurableOrchestrationContext,
) -> dict[str, Any]:
    request: dict[str, Any] = context.get_input() or {}

    # 1. RG
    context.set_custom_status({"phase": "rg", "step": 1, "description": "Creating resource group"})
    yield context.call_activity("ensure_resource_group_activity", request)

    # 2. Network (depends only on RG)
    context.set_custom_status({"phase": "network", "step": 2, "description": "Setting up network"})
    network = yield context.call_activity("ensure_network_activity", request)

    # 3. Key Vault (depends only on RG)
    context.set_custom_status({"phase": "keyvault", "step": 3, "description": "Creating Key Vault"})
    kv_info = yield context.call_activity("ensure_keyvault_activity", request)

    # 4. Password (needs Key Vault)
    context.set_custom_status({"phase": "password", "step": 4, "description": "Generating admin password"})
    password_payload = {**request, "vault_uri": kv_info["vault_uri"]}
    password_info = yield context.call_activity("generate_password_activity", password_payload)

    # 5. VM (needs NIC + password)
    context.set_custom_status({"phase": "vm", "step": 5, "description": "Creating VM"})
    vm_payload = {
        **request,
        "nic_id": network["nic_id"],
        "admin_password": password_info["password"],
    }
    vm = yield context.call_activity("create_vm_activity", vm_payload)

    # 6. Poll cloud-init (Run Command can fail before VM agent is ready, so tolerate errors)
    cloud_init_status = "unknown"
    for attempt in range(CLOUD_INIT_MAX_ATTEMPTS):
        next_check = context.current_utc_datetime + timedelta(
            seconds=CLOUD_INIT_POLL_INTERVAL_SECONDS
        )
        yield context.create_timer(next_check)
        try:
            check = yield context.call_activity("check_cloud_init_activity", request)
            cloud_init_status = check.get("status", "unknown")
        except Exception as exc:
            # #21: Distinguish transient (VM agent not ready) from permanent errors
            exc_str = str(exc).lower()
            if "not found" in exc_str or "authorization" in exc_str:
                cloud_init_status = "error"
                LOGGER.warning("cloud-init check permanent error: %s", exc)
                break
            # Transient — VM agent may still be starting
            cloud_init_status = "running"
        if cloud_init_status in ("done", "failed"):
            break
        context.set_custom_status(
            {"phase": "cloud-init", "attempt": attempt + 1, "status": cloud_init_status}
        )

    return {
        "vm_name": request["vm_name"],
        "resource_group": request["resource_group"],
        "region": request["region"],
        "fqdn": network["fqdn"],
        "ssh_host": network["fqdn"] or network["public_ip_address"],
        "ssh_port": 22,
        "username": request["admin_username"],
        "password_secret_uri": password_info["secret_uri"],
        "cloud_init_status": cloud_init_status,
        "vm": vm,
    }
