"""VM creation + Run Command for Remote Terminal."""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass

from azure.core.credentials import TokenCredential

from services.azure_clients import compute_client

LOGGER = logging.getLogger(__name__)

UBUNTU_IMAGE = {
    "publisher": "Canonical",
    "offer": "0001-com-ubuntu-server-jammy",
    "sku": "22_04-lts-gen2",
    "version": "latest",
}


@dataclass(frozen=True)
class VmInfo:
    vm_id: str
    name: str
    provisioning_state: str


def create_terminal_vm(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    region: str,
    vm_name: str,
    vm_size: str,
    admin_username: str,
    admin_password: str,
    nic_id: str,
    cloud_init_yaml: str,
) -> VmInfo:
    """Create the Remote Terminal VM with cloud-init custom data. Idempotent."""
    cc = compute_client(credential, subscription_id)
    LOGGER.info("create_terminal_vm name=%s size=%s", vm_name, vm_size)
    custom_data = base64.b64encode(cloud_init_yaml.encode("utf-8")).decode("ascii")

    poller = cc.virtual_machines.begin_create_or_update(
        resource_group,
        vm_name,
        {
            "location": region,
            "hardware_profile": {"vm_size": vm_size},
            "storage_profile": {
                "image_reference": UBUNTU_IMAGE,
                "os_disk": {
                    "create_option": "FromImage",
                    "disk_size_gb": 64,
                    "managed_disk": {"storage_account_type": "Premium_LRS"},
                },
            },
            "os_profile": {
                "computer_name": vm_name,
                "admin_username": admin_username,
                "admin_password": admin_password,
                "linux_configuration": {
                    "disable_password_authentication": False,
                    "patch_settings": {"patch_mode": "AutomaticByPlatform"},
                },
                "custom_data": custom_data,
            },
            "network_profile": {
                "network_interfaces": [{"id": nic_id, "properties": {"primary": True}}],
            },
        },
    )
    vm = poller.result()
    return VmInfo(
        vm_id=vm.id,
        name=vm.name,
        provisioning_state=vm.provisioning_state or "Unknown",
    )


def run_shell(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    vm_name: str,
    script: str,
) -> str:
    """Execute a shell snippet via the Run Command extension. Returns stdout+stderr."""
    cc = compute_client(credential, subscription_id)
    poller = cc.virtual_machines.begin_run_command(
        resource_group,
        vm_name,
        {"command_id": "RunShellScript", "script": [script]},
    )
    result = poller.result()
    parts = []
    for item in result.value or []:
        if item.message:
            parts.append(item.message)
    return "\n".join(parts)
