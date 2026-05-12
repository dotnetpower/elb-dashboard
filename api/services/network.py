"""Idempotent helpers for resource group + VNet + NSG + Public IP."""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass

from azure.core.credentials import TokenCredential

from services.azure_clients import network_client, resource_client

LOGGER = logging.getLogger(__name__)

VNET_NAME_TEMPLATE = "vnet-{vm_name}"
SUBNET_NAME = "default"
NSG_NAME_TEMPLATE = "nsg-{vm_name}"
PIP_NAME_TEMPLATE = "pip-{vm_name}"
NIC_NAME_TEMPLATE = "nic-{vm_name}"


def _dns_label(subscription_id: str, resource_group: str, vm_name: str) -> str:
    """Compute a region-unique DNS label.

    Azure DNS labels are unique per region across the entire cloud, so a
    naive ``elb-term-<vm>`` collides as soon as two resource groups try
    to provision a terminal with the default VM name. Suffix the label
    with a short stable hash of the (subscription, RG, VM) tuple so each
    provisioning target gets its own FQDN.
    """
    base = re.sub(r"[^a-z0-9-]", "-", vm_name.lower()).strip("-") or "vm"
    digest = hashlib.sha256(
        f"{subscription_id}|{resource_group}|{vm_name}".lower().encode("utf-8")
    ).hexdigest()[:6]
    label = f"elb-term-{base}-{digest}"
    # DNS labels must be 1..63 chars, lowercase alphanumeric or '-'.
    return label[:63].rstrip("-")


@dataclass(frozen=True)
class NetworkInfo:
    nic_id: str
    public_ip_id: str
    public_ip_address: str
    fqdn: str


def ensure_resource_group(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    region: str,
) -> None:
    """Create or update the resource group. Idempotent."""
    rc = resource_client(credential, subscription_id)
    LOGGER.info("ensure_resource_group rg=%s region=%s", resource_group, region)
    rc.resource_groups.create_or_update(
        resource_group,
        {"location": region, "tags": {"managed-by": "elastic-blast-azure-functionapp"}},
    )


def ensure_network(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    region: str,
    vm_name: str,
    allowed_ssh_cidr: str,
) -> NetworkInfo:
    """Create VNet + subnet + NSG + Public IP + NIC. Idempotent."""
    nc = network_client(credential, subscription_id)
    vnet_name = VNET_NAME_TEMPLATE.format(vm_name=vm_name)
    nsg_name = NSG_NAME_TEMPLATE.format(vm_name=vm_name)
    pip_name = PIP_NAME_TEMPLATE.format(vm_name=vm_name)
    nic_name = NIC_NAME_TEMPLATE.format(vm_name=vm_name)
    dns_label = _dns_label(subscription_id, resource_group, vm_name)

    LOGGER.info(
        "ensure_network vnet=%s nsg=%s pip=%s nic=%s dns=%s",
        vnet_name, nsg_name, pip_name, nic_name, dns_label,
    )

    nsg_poller = nc.network_security_groups.begin_create_or_update(
        resource_group,
        nsg_name,
        {
            "location": region,
            "security_rules": [
                {
                    "name": "AllowSshFromCaller",
                    "protocol": "Tcp",
                    "source_port_range": "*",
                    "destination_port_range": "22",
                    "source_address_prefix": allowed_ssh_cidr,
                    "destination_address_prefix": "*",
                    "access": "Allow",
                    "priority": 1000,
                    "direction": "Inbound",
                },
                {
                    "name": "AllowOutboundAzure",
                    "protocol": "*",
                    "source_port_range": "*",
                    "destination_port_range": "*",
                    "source_address_prefix": "*",
                    "destination_address_prefix": "AzureCloud",
                    "access": "Allow",
                    "priority": 1000,
                    "direction": "Outbound",
                },
                {
                    "name": "DenyOtherOutbound",
                    "protocol": "*",
                    "source_port_range": "*",
                    "destination_port_range": "*",
                    "source_address_prefix": "*",
                    "destination_address_prefix": "*",
                    "access": "Deny",
                    "priority": 4000,
                    "direction": "Outbound",
                },
            ],
        },
    )
    nsg = nsg_poller.result()

    vnet_poller = nc.virtual_networks.begin_create_or_update(
        resource_group,
        vnet_name,
        {
            "location": region,
            "address_space": {"address_prefixes": ["10.42.0.0/16"]},
            "subnets": [
                {
                    "name": SUBNET_NAME,
                    "address_prefix": "10.42.1.0/24",
                    "network_security_group": {"id": nsg.id},
                }
            ],
        },
    )
    vnet = vnet_poller.result()
    subnet_id = vnet.subnets[0].id

    # Public IP — if it already exists with the same DNS label, reuse it.
    # Re-PUTting can spuriously fail with DnsRecordInUse on some regions/SKUs
    # because Azure interprets the request as a DNS reservation conflict
    # against itself. Reading the existing resource is always idempotent.
    from azure.core.exceptions import ResourceNotFoundError

    pip = None
    try:
        existing_pip = nc.public_ip_addresses.get(resource_group, pip_name)
        existing_label = (
            existing_pip.dns_settings.domain_name_label if existing_pip.dns_settings else None
        )
        if existing_label == dns_label and existing_pip.location == region:
            LOGGER.info("ensure_network reusing existing public IP %s (label=%s)", pip_name, dns_label)
            pip = existing_pip
    except ResourceNotFoundError:
        pass

    if pip is None:
        pip_poller = nc.public_ip_addresses.begin_create_or_update(
            resource_group,
            pip_name,
            {
                "location": region,
                "sku": {"name": "Standard"},
                "public_ip_allocation_method": "Static",
                "dns_settings": {"domain_name_label": dns_label},
            },
        )
        pip = pip_poller.result()

    nic_poller = nc.network_interfaces.begin_create_or_update(
        resource_group,
        nic_name,
        {
            "location": region,
            "ip_configurations": [
                {
                    "name": "ipconfig1",
                    "subnet": {"id": subnet_id},
                    "public_ip_address": {"id": pip.id},
                }
            ],
        },
    )
    nic = nic_poller.result()

    return NetworkInfo(
        nic_id=nic.id,
        public_ip_id=pip.id,
        public_ip_address=pip.ip_address or "",
        fqdn=pip.dns_settings.fqdn if pip.dns_settings else "",
    )


def create_ssh_rule(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    nsg_name: str,
    caller_ip: str,
) -> None:
    """Create or update an NSG rule to allow SSH from a specific IP."""
    nc = network_client(credential, subscription_id)
    nc.security_rules.begin_create_or_update(
        resource_group, nsg_name, "AllowSSH",
        {
            "protocol": "Tcp",
            "source_port_range": "*",
            "destination_port_range": "22",
            "source_address_prefix": f"{caller_ip}/32",
            "destination_address_prefix": "*",
            "access": "Allow",
            "priority": 100,
            "direction": "Inbound",
        },
    ).result()


def delete_resource(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    resource_type: str,
    name: str,
) -> None:
    """Delete a network resource (nic, pip, nsg) by type."""
    nc = network_client(credential, subscription_id)
    if resource_type == "nic":
        nc.network_interfaces.begin_delete(resource_group, name).result()
    elif resource_type == "pip":
        nc.public_ip_addresses.begin_delete(resource_group, name).result()
    elif resource_type == "nsg":
        nc.network_security_groups.begin_delete(resource_group, name).result()
    LOGGER.info("Deleted %s/%s in %s", resource_type, name, resource_group)


def delete_resource_group(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
) -> None:
    """Delete a resource group."""
    rc = resource_client(credential, subscription_id)
    rc.resource_groups.begin_delete(resource_group).result()
    LOGGER.info("Deleted resource group %s", resource_group)
