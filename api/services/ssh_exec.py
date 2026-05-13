"""SSH command execution on the Remote Terminal VM.

Replaces Azure VM Run Command for elastic-blast prepare/submit/status/delete.
Run Command has 30-60s overhead per call; SSH completes in 1-2s.

Security:
- Password is retrieved from Key Vault at call time (never stored in memory).
- Connection is closed after each command (no persistent sessions).
- Host key checking is disabled for simplicity (VM is ephemeral and IP changes).
  In production, use known_hosts or Azure Bastion tunneling.
"""

from __future__ import annotations

import logging

LOGGER = logging.getLogger(__name__)

# Default connection parameters
DEFAULT_SSH_PORT = 22
DEFAULT_SSH_TIMEOUT = 120  # seconds — elastic-blast prepare can take ~60s
DEFAULT_USERNAME = "azureuser"


def run_ssh(
    hostname: str,
    password: str,
    script: str,
    *,
    username: str = DEFAULT_USERNAME,
    port: int = DEFAULT_SSH_PORT,
    timeout: int = DEFAULT_SSH_TIMEOUT,
) -> str:
    """Execute a shell script on the VM via SSH. Returns combined stdout+stderr.

    Much faster than Azure VM Run Command (~1-2s overhead vs ~30-60s).
    """
    import paramiko

    client = paramiko.SSHClient()
    # Accept host keys but log a warning: VMs are ephemeral and IPs change.
    # WarningPolicy logs unknown keys rather than silently trusting (AutoAddPolicy).
    client.set_missing_host_key_policy(paramiko.WarningPolicy())  # noqa: S507

    try:
        LOGGER.info("SSH connecting to %s@%s:%d", username, hostname, port)
        client.connect(
            hostname,
            port=port,
            username=username,
            password=password,
            timeout=5,
            banner_timeout=10,
            auth_timeout=10,
            allow_agent=False,
            look_for_keys=False,
        )
        # Enable keepalive to detect dead connections early
        transport = client.get_transport()
        if transport:
            transport.set_keepalive(30)

        # Execute with a generous timeout for long-running commands
        _stdin, stdout, stderr = client.exec_command(
            script,
            timeout=timeout,
            get_pty=False,
        )

        # Read output
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        exit_code = stdout.channel.recv_exit_status()

        combined = out
        if err:
            combined += "\n" + err
        combined += f"\nEXIT_CODE={exit_code}"

        LOGGER.info(
            "SSH command completed on %s (exit=%d, out=%d bytes)",
            hostname,
            exit_code,
            len(out),
        )
        return combined

    except Exception as exc:
        LOGGER.error("SSH execution failed on %s: %s", hostname, exc)
        raise
    finally:
        try:
            client.close()
        except Exception:
            LOGGER.debug("SSH close failed (non-fatal)")


def get_vm_ssh_info(
    credential,
    subscription_id: str,
    resource_group: str,
    vm_name: str,
) -> str | None:
    """Get the public IP address of a VM for SSH connection.

    Caches the result in a module-level dict to avoid repeated Azure API calls
    (~6s for vm.get + nic.get + pip.get).
    """
    cache_key = f"{subscription_id}/{resource_group}/{vm_name}"
    if cache_key in _VM_IP_CACHE:
        LOGGER.debug("SSH IP cache hit for %s: %s", vm_name, _VM_IP_CACHE[cache_key])
        return _VM_IP_CACHE[cache_key]

    from services.azure_clients import compute_client, network_client

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

        ip = pip.ip_address
        _VM_IP_CACHE[cache_key] = ip
        return ip
    except Exception as exc:
        LOGGER.warning("Could not get SSH info for %s: %s", vm_name, exc)
        return None


# Module-level cache for VM IP addresses (cleared on Function App restart)
_VM_IP_CACHE: dict[str, str] = {}
