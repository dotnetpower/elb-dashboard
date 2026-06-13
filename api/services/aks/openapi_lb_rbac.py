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

# Recovery affordance the SPA renders when the LB-pending cause is the missing
# node-subnet RBAC (distinct from the generic VNet-peering hint). The SPA wires
# the "Grant LB subnet RBAC" button to POST /api/aks/openapi/lb-subnet-rbac.
LB_SUBNET_RBAC_RECOVERY_ACTION = "grant_lb_subnet_rbac"
_LB_SUBNET_RBAC_RECOVERY_HINT = (
    "The elb-openapi internal LoadBalancer cannot get an IP because the AKS "
    "cluster identity lacks Network Contributor on its node subnet "
    "(SyncLoadBalancerFailed / AuthorizationFailed on subnets/...). Click "
    "'Grant LB subnet RBAC' to grant it (idempotent), then wait a few minutes "
    "or stop/start the cluster for the cloud-controller to pick it up."
)


def lb_subnet_rbac_recovery_hint() -> dict[str, str]:
    """Additive ``recovery_action`` / ``recovery_hint`` pair for the SPA.

    Returned alongside a degraded ``openapi`` spec/proxy payload when the
    LB-pending cause is classified as the missing node-subnet RBAC, so the SPA
    can render the correct one-click fix instead of the generic peering hint.
    """
    return {
        "recovery_action": LB_SUBNET_RBAC_RECOVERY_ACTION,
        "recovery_hint": _LB_SUBNET_RBAC_RECOVERY_HINT,
    }


def detect_lb_subnet_rbac_missing(
    cred: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
) -> bool:
    """Best-effort: does the elb-openapi Service have an LB subnet-403 event?

    Reads recent ``default`` namespace events and returns True when the
    ``elb-openapi`` Service shows a ``SyncLoadBalancerFailed`` whose message is
    an authorization failure on a subnet read — the exact signature of the
    cluster identity missing Network Contributor on the BYO node subnet
    (GitHub #33). Returns False on any read error or when no such event exists,
    so the caller degrades to the generic hint rather than misclassifying.

    Only called when the LoadBalancer IP is already known to be missing, so it
    adds at most one events read on an already-degraded path (never on the
    healthy path).
    """
    try:
        from api.services.k8s.observability import k8s_list_events

        events = k8s_list_events(
            cred,
            subscription_id,
            resource_group,
            cluster_name,
            namespace="default",
            limit=50,
        )
    except Exception:
        LOGGER.debug("lb subnet rbac detection: events read failed", exc_info=True)
        return False

    for event in events:
        if not isinstance(event, dict):
            continue
        involved = str(event.get("involved_name") or "").strip()
        reason = str(event.get("reason") or "")
        if involved != "elb-openapi" or "SyncLoadBalancerFailed" not in reason:
            continue
        message = str(event.get("message") or "").lower()
        if "subnet" in message and (
            "authorizationfailed" in message
            or "403" in message
            or "does not have authorization" in message
        ):
            return True
    return False


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
