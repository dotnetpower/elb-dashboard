"""Grant the cluster identity Network Contributor on its BYO node subnet so the
Azure cloud-provider can allocate the ``elb-openapi`` internal LoadBalancer IP.

Responsibility: Reusable recovery for the BYO-subnet RBAC gap that leaves the
    ``elb-openapi`` internal LoadBalancer stuck ``<pending>`` with a
    ``subnets/<snet> ... 403 AuthorizationFailed`` event. Resolves the cluster
    control-plane identity + its node subnet and (idempotently) grants Network
    Contributor on that subnet — the same grant ``provision_aks`` performs at
    create time, re-runnable for clusters created out-of-band (manual
    ``az aks create``). See GitHub issue #33.
Edit boundaries: Reusable domain/Azure logic only. No HTTP shaping (that is
    ``api.routes.aks.openapi``), no Celery task body. The actual role-assignment
    write is delegated to ``api.tasks.azure.rbac.grant_network_contributor_on_subnet``
    (stable-UUID idempotent), and the subnet resolution to
    ``api.services.aks.node_subnet_nsg.first_node_subnet_id``.
Key entry points: ``ensure_openapi_lb_subnet_rbac``.
Risky contracts: The runtime LoadBalancer reconcile runs as the cluster
    control-plane identity, NOT the dashboard MI that created the nodes, so the
    grant target is ``cluster.identity`` (SystemAssigned ``principal_id`` or the
    first UserAssigned principal). Granting on an already-running cluster does
    NOT take effect immediately: the cloud-controller caches its ARM token, so
    the LoadBalancer can stay ``<pending>`` for several minutes until the token
    refreshes — a cluster stop/start forces pickup. Callers MUST surface the
    returned ``note`` so an operator does not read the delay as a failure.
    Managed-VNet clusters (no ``vnet_subnet_id``) are a graceful skip; the grant
    is additive (never narrows a role) so it is safe to re-run.
Validation: ``uv run pytest -q api/tests/test_openapi_lb_subnet_rbac.py``.
"""

from __future__ import annotations

import logging
from typing import Any

from azure.core.credentials import TokenCredential

from api.services.aks.node_subnet_nsg import first_node_subnet_id

LOGGER = logging.getLogger(__name__)

# Surfaced to the operator verbatim. The cloud-controller caches its ARM/MSI
# credential, so a freshly granted role is not seen until that credential
# refreshes; a cluster stop/start forces a fresh one. Provision-time grants
# avoid this because they run while the cluster is being created.
CLOUD_CONTROLLER_CACHE_NOTE = (
    "Network Contributor granted on the node subnet. The AKS cloud-controller "
    "caches its ARM credential, so the elb-openapi internal LoadBalancer may "
    "stay <pending> for several minutes until it refreshes. If it does not "
    "recover on its own, stop and start the cluster to force the "
    "cloud-controller to pick up the new role."
)


def _resolve_cluster_identity_principal(cluster: Any) -> str:
    """Return the cluster control-plane identity ``principal_id``, or "".

    Handles both SystemAssigned (``identity.principal_id``) and UserAssigned
    (first entry of ``identity.user_assigned_identities``) control-plane
    identities. Returns "" when the cluster has no resolvable identity (very
    old service-principal clusters), which the caller treats as a skip.
    """
    identity = getattr(cluster, "identity", None)
    if identity is None:
        return ""
    principal = (getattr(identity, "principal_id", None) or "").strip()
    if principal:
        return principal
    uami = getattr(identity, "user_assigned_identities", None) or {}
    for value in uami.values():
        pid = (getattr(value, "principal_id", None) or "").strip()
        if pid:
            return pid
    return ""


def ensure_openapi_lb_subnet_rbac(
    cred: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    *,
    cluster: Any = None,
) -> dict[str, Any]:
    """Grant Network Contributor on the cluster's BYO node subnet (idempotent).

    Mirrors the provision-time grant for clusters created out-of-band. Returns
    a status dict the route surfaces verbatim:

    * ``{"status": "skipped", "reason": "cluster_identity_unresolved"}`` — the
      cluster has no managed identity to grant to.
    * ``{"status": "skipped", "reason": "managed_vnet_mode"}`` — not a BYO-subnet
      cluster, so AKS manages the LoadBalancer NSG/route itself; nothing to do.
    * ``{"status": "granted", "principal_id", "subnet_id", "role", "note"}`` —
      the grant now exists (or already existed). ``note`` carries the
      token-cache caveat the operator must see.

    ``cluster`` lets a caller that already fetched the ``ManagedCluster`` (e.g.
    ``deploy_openapi_service``) pass it in to avoid a duplicate ARM
    ``managed_clusters.get``; when ``None`` the cluster is fetched here.
    """
    from api.tasks.azure import _grant_network_contributor_on_subnet

    if cluster is None:
        from api.services.azure_clients import aks_client

        cluster = aks_client(cred, subscription_id).managed_clusters.get(
            resource_group, cluster_name
        )

    principal = _resolve_cluster_identity_principal(cluster)
    if not principal:
        LOGGER.info(
            "openapi LB subnet RBAC: cluster %s has no resolvable identity, skipping",
            cluster_name,
        )
        return {"status": "skipped", "reason": "cluster_identity_unresolved"}

    subnet_id = first_node_subnet_id(cluster)
    if not subnet_id:
        LOGGER.info(
            "openapi LB subnet RBAC: cluster %s is managed-VNet, skipping",
            cluster_name,
        )
        return {"status": "skipped", "reason": "managed_vnet_mode"}

    _grant_network_contributor_on_subnet(
        cred,
        subscription_id,
        principal_id=principal,
        subnet_id=subnet_id,
        label=f"{cluster_name} cluster identity (openapi LB recovery)",
    )
    LOGGER.info(
        "openapi LB subnet RBAC ensured for cluster %s on subnet %s",
        cluster_name,
        subnet_id,
    )
    return {
        "status": "granted",
        "principal_id": principal,
        "subnet_id": subnet_id,
        "role": "Network Contributor",
        "note": CLOUD_CONTROLLER_CACHE_NOTE,
    }
