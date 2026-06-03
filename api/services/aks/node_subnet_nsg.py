"""Reconcile the AKS node-subnet NSG so the ingress LoadBalancer is reachable.

Responsibility: Ensure the network security group attached to an AKS cluster's
    BYO node subnet carries an inbound Allow rule for Internet -> 80/443 to the
    ingress-nginx LoadBalancer VIP, so the public HTTPS path (Let's Encrypt
    HTTP-01 challenge + serving) actually reaches the controller.
Edit boundaries: Pure Azure network reconcile. No kubectl, no manifest building
    (that is `api.services.k8s.ingress`), no task orchestration (that is
    `api.tasks.openapi.public_https`). Keep it idempotent and side-effect-only.
Key entry points: `ensure_ingress_lb_inbound_rule`,
    `first_node_subnet_id`.
Risky contracts: AKS's cloud-controller-manager writes the matching LB inbound
    rule ONLY to the NIC/cluster NSG in the MC_ node resource group, NOT to a
    BYO subnet NSG. When the node subnet has its own NSG (the dashboard's
    `vnet-elb-dashboard/snet-aks` BYO model), its default `DenyAllInBound`
    silently drops the inbound 80/443 and the ACME challenge times out. This
    helper closes that gap. It is a no-op (graceful skip) for managed-VNet
    clusters and for BYO subnets that have no NSG attached, so it can never
    regress an already-working cluster. The rule is destination-scoped to the
    exact LB VIP and only opens 80/443 — never a wider surface.
Validation: `uv run pytest -q api/tests/test_node_subnet_nsg.py`.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from azure.core.credentials import TokenCredential

from api.services.azure_clients import network_client

LOGGER = logging.getLogger(__name__)

# Fixed rule name so re-running the pipeline updates the rule in place
# (idempotent) instead of stacking duplicates.
INGRESS_LB_INBOUND_RULE_NAME = "allow-ingress-nginx-http-https"
# Priority well below the default DenyAllInBound (65500) and matching the
# convention AKS uses for its own LB rule on the NIC NSG. The dashboard's
# BYO node subnet NSG ships with no custom rules, so 500 is collision-free.
INGRESS_LB_INBOUND_RULE_PRIORITY = 500

_SUBNET_ID_RE = re.compile(
    r"/subscriptions/(?P<sub>[^/]+)/resourceGroups/(?P<rg>[^/]+)"
    r"/providers/Microsoft\.Network/virtualNetworks/(?P<vnet>[^/]+)"
    r"/subnets/(?P<subnet>[^/]+)",
    re.IGNORECASE,
)
_NSG_ID_RE = re.compile(
    r"/subscriptions/[^/]+/resourceGroups/(?P<rg>[^/]+)"
    r"/providers/Microsoft\.Network/networkSecurityGroups/(?P<nsg>[^/]+)",
    re.IGNORECASE,
)


def first_node_subnet_id(cluster: Any) -> str:
    """Return the first non-empty agent-pool `vnet_subnet_id`, or "".

    A managed-VNet AKS cluster has no `vnet_subnet_id` on its agent pools, so
    this returns "" and the caller treats it as "managed VNet — nothing to do"
    (AKS already manages the NIC NSG correctly in that mode).
    """
    profiles = getattr(cluster, "agent_pool_profiles", None) or []
    for profile in profiles:
        subnet_id = (getattr(profile, "vnet_subnet_id", None) or "").strip()
        if subnet_id:
            return subnet_id
    return ""


def ensure_ingress_lb_inbound_rule(
    *,
    credential: TokenCredential,
    subscription_id: str,
    node_subnet_id: str,
    lb_ip: str,
) -> dict[str, str]:
    """Ensure Internet -> 80/443 to `lb_ip` is allowed on the node-subnet NSG.

    Returns a small status dict the caller logs:

    * ``{"status": "skipped", "reason": "managed_vnet"}`` — no BYO subnet.
    * ``{"status": "skipped", "reason": "no_subnet_nsg"}`` — BYO subnet has no
      NSG attached, so AKS's NIC-NSG rule is the only gate and already allows
      the traffic.
    * ``{"status": "ensured", "nsg": ..., "rule": ..., "lb_ip": ...}`` — the
      allow rule now exists.

    Idempotent: uses a fixed rule name + `begin_create_or_update`, so a second
    run updates the same rule in place. The helper never widens the surface
    beyond the exact LB VIP on ports 80/443.
    """
    node_subnet_id = (node_subnet_id or "").strip()
    if not node_subnet_id:
        return {"status": "skipped", "reason": "managed_vnet"}
    if not (lb_ip or "").strip():
        raise ValueError("lb_ip is required to scope the ingress NSG rule")

    match = _SUBNET_ID_RE.search(node_subnet_id)
    if not match:
        raise ValueError(f"cannot parse node subnet id: {node_subnet_id!r}")
    subnet_sub = match.group("sub")
    subnet_rg = match.group("rg")
    vnet_name = match.group("vnet")
    subnet_name = match.group("subnet")

    # The node subnet lives in the platform/workload VNet, which may be in a
    # different subscription than the cluster object. Use the subnet's own
    # subscription for every network call below.
    client = network_client(credential, subnet_sub)
    subnet = client.subnets.get(subnet_rg, vnet_name, subnet_name)
    nsg_ref = getattr(subnet, "network_security_group", None)
    nsg_id = (getattr(nsg_ref, "id", None) or "").strip() if nsg_ref else ""
    if not nsg_id:
        return {"status": "skipped", "reason": "no_subnet_nsg"}

    nsg_match = _NSG_ID_RE.search(nsg_id)
    if not nsg_match:
        raise ValueError(f"cannot parse node-subnet NSG id: {nsg_id!r}")
    nsg_rg = nsg_match.group("rg")
    nsg_name = nsg_match.group("nsg")

    LOGGER.info(
        "public-https: ensuring node-subnet NSG inbound rule nsg=%s rule=%s "
        "lb_ip=%s ports=80,443",
        nsg_name,
        INGRESS_LB_INBOUND_RULE_NAME,
        lb_ip,
    )
    client.security_rules.begin_create_or_update(
        nsg_rg,
        nsg_name,
        INGRESS_LB_INBOUND_RULE_NAME,
        {
            "priority": INGRESS_LB_INBOUND_RULE_PRIORITY,
            "direction": "Inbound",
            "access": "Allow",
            "protocol": "Tcp",
            "source_address_prefix": "Internet",
            "source_port_range": "*",
            "destination_address_prefix": lb_ip,
            "destination_port_ranges": ["80", "443"],
            "description": (
                "Allow Internet 80/443 to ingress-nginx LB VIP for public "
                "HTTPS (BYO node-subnet NSG; AKS only manages the NIC NSG)."
            ),
        },
    ).result()

    return {
        "status": "ensured",
        "nsg": nsg_name,
        "rule": INGRESS_LB_INBOUND_RULE_NAME,
        "lb_ip": lb_ip,
    }
