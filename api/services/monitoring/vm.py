"""VM status helpers (legacy bridge endpoints).

Responsibility: VM status helpers (legacy bridge endpoints).
Edit boundaries: Keep reusable domain logic here; routes and tasks call this layer.
Key entry points: _resolve_vm_public_endpoint, get_vm_status
Risky contracts: Keep Azure credentials centralized and sanitise data at HTTP/log boundaries.
Validation: `uv run pytest -q api/tests`.
"""

from __future__ import annotations

import logging
from typing import Any

from azure.core.credentials import TokenCredential

from api.services.azure_clients import compute_client

LOGGER = logging.getLogger(__name__)


def _resolve_vm_public_endpoint(
    credential: TokenCredential,
    subscription_id: str,
    vm: Any,
) -> tuple[str | None, str | None]:
    try:
        from azure.mgmt.network import NetworkManagementClient

        if not vm.network_profile or not vm.network_profile.network_interfaces:
            return None, None
        network_client = NetworkManagementClient(credential, subscription_id)
        nic_id = vm.network_profile.network_interfaces[0].id
        nic_parts = nic_id.split("/")
        nic_rg = nic_parts[nic_parts.index("resourceGroups") + 1]
        nic_name = nic_parts[-1]
        nic = network_client.network_interfaces.get(nic_rg, nic_name)
        if not nic.ip_configurations:
            return None, None
        public_ip_ref = nic.ip_configurations[0].public_ip_address
        if not public_ip_ref or not public_ip_ref.id:
            return None, None
        public_ip_parts = public_ip_ref.id.split("/")
        public_ip_rg = public_ip_parts[public_ip_parts.index("resourceGroups") + 1]
        public_ip_name = public_ip_parts[-1]
        public_ip = network_client.public_ip_addresses.get(public_ip_rg, public_ip_name)
        fqdn = public_ip.dns_settings.fqdn if public_ip.dns_settings else None
        return public_ip.ip_address, fqdn
    except Exception as exc:
        LOGGER.debug("could not resolve public IP for %s: %s", vm.name, exc)
        return None, None


# ---------------------------------------------------------------------------
# Resource creation (idempotent)
# ---------------------------------------------------------------------------


def get_vm_status(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    vm_name: str,
) -> dict[str, Any]:
    client = compute_client(credential, subscription_id)
    vm = client.virtual_machines.get(resource_group, vm_name, expand="instanceView")
    statuses = vm.instance_view.statuses if vm.instance_view else []
    power_state = next(
        (
            status.display_status
            for status in statuses
            if status.code and status.code.startswith("PowerState/")
        ),
        None,
    )

    os_disk_gb: int | None = None
    if vm.storage_profile and vm.storage_profile.os_disk:
        os_disk_gb = vm.storage_profile.os_disk.disk_size_gb

    identity_type: str | None = None
    has_managed_identity = False
    if vm.identity:
        identity_type = vm.identity.type
        has_managed_identity = identity_type in (
            "SystemAssigned",
            "SystemAssigned, UserAssigned",
        )

    public_ip, fqdn = _resolve_vm_public_endpoint(credential, subscription_id, vm)

    return {
        "name": vm.name,
        "region": vm.location,
        "vm_size": vm.hardware_profile.vm_size if vm.hardware_profile else None,
        "provisioning_state": vm.provisioning_state,
        "power_state": power_state,
        "os_disk_gb": os_disk_gb,
        "public_ip": public_ip,
        "fqdn": fqdn,
        "has_managed_identity": has_managed_identity,
        "identity_type": identity_type,
    }
