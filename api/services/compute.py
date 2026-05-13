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
    principal_id: str | None = None


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
            "identity": {"type": "SystemAssigned"},
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
    # Extract SystemAssigned managed identity principal ID
    principal_id = None
    if vm.identity and vm.identity.principal_id:
        principal_id = vm.identity.principal_id
    return VmInfo(
        vm_id=vm.id,
        name=vm.name,
        provisioning_state=vm.provisioning_state or "Unknown",
        principal_id=principal_id,
    )


def run_shell(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    vm_name: str,
    script: str,
    max_retries: int = 3,
    *,
    ssh_password: str | None = None,
) -> str:
    """Execute a shell snippet on a VM. Returns stdout+stderr.

    Strategy: SSH first (1-2s overhead), fallback to Run Command (30-60s) if SSH fails.
    Pass ssh_password to enable SSH. Without it, falls back to Run Command directly.
    """
    # Try SSH first if password is available
    if ssh_password:
        try:
            from services.ssh_exec import run_ssh, get_vm_ssh_info
            ip = get_vm_ssh_info(credential, subscription_id, resource_group, vm_name)
            if ip:
                return run_ssh(ip, ssh_password, script)
            else:
                LOGGER.warning("No public IP for %s — falling back to Run Command", vm_name)
        except Exception as exc:
            LOGGER.warning("SSH failed for %s (%s) — falling back to Run Command", vm_name, exc)

    # Fallback: Run Command
    return _run_command(credential, subscription_id, resource_group, vm_name, script, max_retries)


def _run_command(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    vm_name: str,
    script: str,
    max_retries: int = 3,
) -> str:
    """Execute via Azure VM Run Command extension. Slow but always works."""
    import time as _time
    cc = compute_client(credential, subscription_id)
    last_exc = None
    for attempt in range(max_retries):
        try:
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
        except Exception as exc:
            last_exc = exc
            if "Conflict" in str(exc) or "in progress" in str(exc):
                wait = (attempt + 1) * 15  # 15s, 30s, 45s
                LOGGER.warning("Run Command conflict on %s, retrying in %ds (attempt %d/%d)", vm_name, wait, attempt + 1, max_retries)
                _time.sleep(wait)
            else:
                raise
    raise last_exc  # type: ignore[misc]


def get_vm_public_ip(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    vm_name: str,
) -> str | None:
    """Return the public IP address of a VM, or None if unavailable."""
    from services.azure_clients import network_client

    cc = compute_client(credential, subscription_id)
    nc = network_client(credential, subscription_id)
    try:
        vm = cc.virtual_machines.get(resource_group, vm_name)
        nic_id = vm.network_profile.network_interfaces[0].id
        nic_name = nic_id.split("/")[-1]
        nic_rg = nic_id.split("/")[4]
        nic = nc.network_interfaces.get(nic_rg, nic_name)
        pip_id = nic.ip_configurations[0].public_ip_address.id
        pip_name = pip_id.split("/")[-1]
        pip_rg = pip_id.split("/")[4]
        pip = nc.public_ip_addresses.get(pip_rg, pip_name)
        return pip.ip_address
    except Exception as exc:
        LOGGER.warning("Could not get public IP for %s: %s", vm_name, exc)
        return None


def deallocate_vm(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    vm_name: str,
) -> None:
    """Deallocate (stop) a VM. Idempotent."""
    cc = compute_client(credential, subscription_id)
    cc.virtual_machines.begin_deallocate(resource_group, vm_name).result()
    LOGGER.info("VM %s deallocated in %s", vm_name, resource_group)


def delete_vm(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    vm_name: str,
) -> None:
    """Delete a VM and wait for completion."""
    cc = compute_client(credential, subscription_id)
    cc.virtual_machines.begin_delete(resource_group, vm_name).result()
    LOGGER.info("VM %s deleted in %s", vm_name, resource_group)
