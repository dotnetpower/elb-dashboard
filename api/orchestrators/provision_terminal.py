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

from datetime import timedelta
from typing import Any

import azure.durable_functions as df

CLOUD_INIT_POLL_INTERVAL_SECONDS = 30
CLOUD_INIT_MAX_ATTEMPTS = 30  # 30 * 30s = 15 min hard ceiling


def provision_terminal_orchestrator(
    context: df.DurableOrchestrationContext,
) -> dict[str, Any]:
    request: dict[str, Any] = context.get_input() or {}

    # 1. RG
    yield context.call_activity("ensure_resource_group_activity", request)

    # 2. Network (depends only on RG)
    network = yield context.call_activity("ensure_network_activity", request)

    # 3. Password (independent of network — could fan-out, but keep linear for clarity)
    password_info = yield context.call_activity("generate_password_activity", request)

    # 4. VM (needs NIC + password)
    vm_payload = {
        **request,
        "nic_id": network["nic_id"],
        "admin_password": password_info["password"],
    }
    vm = yield context.call_activity("create_vm_activity", vm_payload)

    # 5. Poll cloud-init (Run Command can fail before VM agent is ready, so tolerate errors)
    cloud_init_status = "unknown"
    for attempt in range(CLOUD_INIT_MAX_ATTEMPTS):
        next_check = context.current_utc_datetime + timedelta(
            seconds=CLOUD_INIT_POLL_INTERVAL_SECONDS
        )
        yield context.create_timer(next_check)
        try:
            check = yield context.call_activity("check_cloud_init_activity", request)
            cloud_init_status = check.get("status", "unknown")
        except Exception:
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
