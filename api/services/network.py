"""Idempotent helpers for resource group + VNet + NSG + Public IP.

Responsibility: Idempotent helpers for resource group + VNet + NSG + Public IP
Edit boundaries: Keep reusable domain logic here; routes and tasks should call this layer
instead of duplicating SDK code.
Key entry points: `_dns_label`, `NetworkInfo`, `ensure_resource_group`, `ensure_network`,
`create_ssh_rule`, `ensure_ssh_from_function_app`
Risky contracts: Keep Azure credentials centralized and sanitise data before HTTP, WebSocket, or
log boundaries.
Validation: `uv run pytest -q api/tests`.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass

from azure.core.credentials import TokenCredential

from api.services.azure_clients import network_client, resource_client

LOGGER = logging.getLogger(__name__)

VNET_NAME_TEMPLATE = "vnet-{vm_name}"
SUBNET_NAME = "default"
NSG_NAME_TEMPLATE = "nsg-{vm_name}"
PIP_NAME_TEMPLATE = "pip-{vm_name}"
NIC_NAME_TEMPLATE = "nic-{vm_name}"
MAX_FUNCTION_SSH_SOURCE_IPS = 64


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
        {"location": region, "tags": {"managed-by": "elb-dashboard"}},
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
        vnet_name,
        nsg_name,
        pip_name,
        nic_name,
        dns_label,
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
                    "priority": 100,
                    "direction": "Inbound",
                },
                {
                    "name": "DenyOtherInbound",
                    "protocol": "*",
                    "source_port_range": "*",
                    "destination_port_range": "*",
                    "source_address_prefix": "*",
                    "destination_address_prefix": "*",
                    "access": "Deny",
                    "priority": 4000,
                    "direction": "Inbound",
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
            LOGGER.info(
                "ensure_network reusing existing public IP %s (label=%s)", pip_name, dns_label
            )
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
        resource_group,
        nsg_name,
        "AllowSSH",
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


def ensure_ssh_from_function_app(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    nsg_name: str,
) -> bool:
    """Ensure NSG allows SSH from this Function App's outbound IPs.

    Creates or updates SSH rules for Function App egress. Idempotent.
    Returns True if at least one rule was ensured.
    """
    import ipaddress
    import os

    import requests as _req

    nc = network_client(credential, subscription_id)
    outbound_ip_candidates: list[str] = []
    for env_name in ("WEBSITE_OUTBOUND_IPS", "WEBSITE_POSSIBLE_OUTBOUND_IPS"):
        outbound_ip_candidates.extend(
            ip.strip() for ip in os.environ.get(env_name, "").split(",") if ip.strip()
        )

    try:
        resp = _req.get(
            "http://169.254.169.254/metadata/instance/network/interface/0/ipv4/ipAddress/0/publicIpAddress?api-version=2021-02-01&format=text",
            headers={"Metadata": "true"},
            timeout=2,
        )
        if resp.status_code == 200 and resp.text.strip():
            outbound_ip_candidates.append(resp.text.strip())
    except Exception as exc:
        LOGGER.info("Could not resolve metadata public IP for Function App egress: %s", exc)

    try:
        resp = _req.get("https://api.ipify.org", timeout=3)
        if resp.status_code == 200 and resp.text.strip():
            outbound_ip_candidates.append(resp.text.strip())
    except Exception as exc:
        LOGGER.info("Could not resolve live Function App egress IP: %s", exc)

    ip_list: list[str] = []
    seen_ips: set[str] = set()
    for ip in outbound_ip_candidates:
        try:
            address = ipaddress.ip_address(ip)
        except ValueError:
            LOGGER.info("Ignoring invalid Function App outbound IP candidate: %s", ip)
            continue
        if address.version != 4 or not address.is_global:
            LOGGER.info("Ignoring non-public Function App outbound IP candidate: %s", ip)
            continue
        normalised = str(address)
        if normalised not in seen_ips:
            seen_ips.add(normalised)
            ip_list.append(normalised)

    if not ip_list:
        LOGGER.warning("Cannot determine Function App outbound IPs for SSH NSG rule")
        return False

    def existing_rule_sources(rule_name: str, destination_port: str) -> set[str]:
        try:
            rule = nc.security_rules.get(resource_group, nsg_name, rule_name)
        except Exception:
            return set()
        if rule.destination_port_range != destination_port:
            return set()
        existing_sources = set(rule.source_address_prefixes or [])
        if rule.source_address_prefix:
            existing_sources.add(rule.source_address_prefix)
        return {source for source in existing_sources if _is_public_ipv4(source)}

    def _is_public_ipv4(source: str) -> bool:
        try:
            address = ipaddress.ip_address(source)
        except ValueError:
            return False
        return address.version == 4 and address.is_global

    def merge_sources(existing_sources: set[str]) -> list[str]:
        merged: list[str] = []
        for source in [*sorted(existing_sources), *ip_list]:
            if source not in merged:
                merged.append(source)
        if len(merged) <= MAX_FUNCTION_SSH_SOURCE_IPS:
            return merged

        required = [source for source in ip_list if source in merged]
        retained: list[str] = []
        for source in required:
            if source not in retained:
                retained.append(source)
        for source in reversed(merged):
            if len(retained) >= MAX_FUNCTION_SSH_SOURCE_IPS:
                break
            if source not in retained:
                retained.append(source)
        LOGGER.warning(
            "Trimming Function App SSH NSG sources from %d to %d entries",
            len(merged),
            len(retained),
        )
        return sorted(retained)

    ensured = False
    try:
        existing_port22_sources = existing_rule_sources("AllowSSH-FunctionApp", "22")
        desired_port22_sources = merge_sources(existing_port22_sources)
        if (
            not set(ip_list).issubset(existing_port22_sources)
            or len(existing_port22_sources) > MAX_FUNCTION_SSH_SOURCE_IPS
        ):
            nc.security_rules.begin_create_or_update(
                resource_group,
                nsg_name,
                "AllowSSH-FunctionApp",
                {
                    "protocol": "Tcp",
                    "source_port_range": "*",
                    "destination_port_range": "22",
                    "source_address_prefixes": desired_port22_sources,
                    "destination_address_prefix": "*",
                    "access": "Allow",
                    "priority": 110,
                    "direction": "Inbound",
                },
            ).result()
            LOGGER.info(
                "Ensured AllowSSH-FunctionApp NSG rule with %d IPs on port 22",
                len(desired_port22_sources),
            )
        ensured = True

        existing_port443_sources = existing_rule_sources("AllowSSH443-FunctionApp", "443")
        desired_port443_sources = merge_sources(existing_port443_sources)
        if (
            not set(ip_list).issubset(existing_port443_sources)
            or len(existing_port443_sources) > MAX_FUNCTION_SSH_SOURCE_IPS
        ):
            nc.security_rules.begin_create_or_update(
                resource_group,
                nsg_name,
                "AllowSSH443-FunctionApp",
                {
                    "protocol": "Tcp",
                    "source_port_range": "*",
                    "destination_port_range": "443",
                    "source_address_prefixes": desired_port443_sources,
                    "destination_address_prefix": "*",
                    "access": "Allow",
                    "priority": 111,
                    "direction": "Inbound",
                },
            ).result()
            LOGGER.info(
                "Ensured AllowSSH443-FunctionApp NSG rule with %d IPs", len(desired_port443_sources)
            )
        ensured = True
        return ensured
    except Exception as exc:
        LOGGER.warning("Failed to create SSH NSG rule: %s", exc)
        return ensured


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
