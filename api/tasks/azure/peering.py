"""Bidirectional VNet peering between the dashboard platform VNet and an AKS cluster VNet.

Responsibility: Stand up the two VNet peerings the api sidecar needs in order to reach an
    AKS internal-LoadBalancer Service IP from outside the AKS-managed VNet (the
    `elb-openapi` proxy / spec / Try-It flow). Idempotent — re-runs against an already
    peered pair are a silent no-op.
Edit boundaries: All VNet-peering writes belong here. Routes and the `provision_aks`
    orchestrator only call into `ensure_vnet_peering_with_cluster`; everything Azure SDK
    related stays in this module.
Key entry points: `ensure_vnet_peering_with_cluster`,
    `_dashboard_vnet_id_from_env`, `_recovery_command`.
Risky contracts: Treats `AlreadyExists` / `Conflict` as success. Any other failure is
    recorded into ``error`` and the caller (typically `provision_aks`) does **not**
    fail the task — the AKS cluster is fully usable; only OpenAPI proxy / spec / Try-It
    is unreachable until peering lands. The returned payload always includes a
    ``recovery_command`` string the SPA / operator can paste.
Validation: `uv run pytest -q api/tests/test_azure_peering.py`.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from api.services.azure_clients import network_client, resource_client

LOGGER = logging.getLogger(__name__)


def _dashboard_vnet_id_from_env() -> str:
    """Resolve the dashboard platform VNet ARM id from container env vars.

    The Container App template (see ``infra/modules/containerAppControl.bicep``)
    injects ``PLATFORM_PRIVATE_ENDPOINT_SUBNET_ID`` on every sidecar. The
    parent VNet id is everything up to ``/subnets/<name>``. Returns ``""``
    when the env var is missing (local-dev shell) so the caller can skip
    instead of crashing.
    """
    subnet_id = (os.environ.get("PLATFORM_PRIVATE_ENDPOINT_SUBNET_ID") or "").strip()
    if not subnet_id:
        return ""
    marker = "/subnets/"
    idx = subnet_id.lower().find(marker)
    if idx < 0:
        return ""
    return subnet_id[:idx]


def _parse_vnet_id(vnet_id: str) -> tuple[str, str, str]:
    """Split a VNet ARM id into ``(subscription_id, resource_group, vnet_name)``.

    Raises ``ValueError`` when the id does not look like a VNet path so a
    bad env / typo surfaces in a single place instead of inside the SDK
    call stack.
    """
    parts = vnet_id.strip("/").split("/")
    # /subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.Network/virtualNetworks/<name>
    if (
        len(parts) < 8
        or parts[0].lower() != "subscriptions"
        or parts[2].lower() != "resourcegroups"
        or parts[6].lower() != "virtualnetworks"
    ):
        raise ValueError(f"not a VNet ARM id: {vnet_id!r}")
    return parts[1], parts[3], parts[7]


def _is_idempotent_conflict(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "alreadyexists" in msg or "conflict" in msg


def _recovery_command(
    *,
    subscription_id: str,
    cluster_resource_group: str,
    cluster_name: str,
) -> str:
    """Render the exact `peer-cluster-network.sh` invocation the operator can run.

    Surfaced verbatim in the helper's return payload so an admin who hits
    the failure path can copy-paste the recovery without guessing names.
    """
    return (
        "bash scripts/dev/peer-cluster-network.sh --yes "
        f"--cluster-rg {cluster_resource_group} "
        f"--cluster-name {cluster_name} "
        f"--subscription {subscription_id}"
    )


def _resolve_aks_node_vnet(
    cred: Any,
    *,
    subscription_id: str,
    node_resource_group: str,
) -> str:
    """Return the AKS-auto-created VNet ARM id, or ``""`` when no VNet lives in MC_*.

    In managed-VNet mode (the default for ``provision_aks``) AKS creates
    exactly one VNet inside the node resource group. BYO-VNet mode would
    keep that RG empty of VNets; in that case the cluster already lives
    in the platform VNet (or another operator-managed VNet) and peering
    from here is either unnecessary or out of scope.
    """
    rc = resource_client(cred, subscription_id)
    vnet_ids: list[str] = []
    try:
        for resource in rc.resources.list_by_resource_group(
            node_resource_group,
            filter="resourceType eq 'Microsoft.Network/virtualNetworks'",
        ):
            res_id = getattr(resource, "id", "") or ""
            if res_id:
                vnet_ids.append(res_id)
    except Exception as exc:
        LOGGER.warning(
            "vnet peering: resource_client.list_by_resource_group(%s) failed: %s",
            node_resource_group,
            exc,
        )
        return ""
    if not vnet_ids:
        return ""
    if len(vnet_ids) > 1:
        # AKS only ever creates one. Multiple VNets in the node RG means
        # the operator added one by hand — surface that ambiguity by
        # picking the first and warning, rather than guessing silently.
        LOGGER.warning(
            "vnet peering: multiple VNets found in %s (%s); using %s",
            node_resource_group,
            len(vnet_ids),
            vnet_ids[0],
        )
    return vnet_ids[0]


def _peering_name(local_vnet_name: str, remote_vnet_name: str) -> str:
    return f"peer-{local_vnet_name}-to-{remote_vnet_name}"


def _create_peering(
    cred: Any,
    *,
    subscription_id: str,
    local_vnet_id: str,
    remote_vnet_id: str,
) -> tuple[str, str]:
    """Create one direction of the peering. Returns ``(name, state)``.

    Idempotent: ``AlreadyExists`` / ``Conflict`` is treated as success and
    re-reads the existing peering's state.
    """
    local_sub, local_rg, local_vnet = _parse_vnet_id(local_vnet_id)
    _remote_sub, _remote_rg, remote_vnet = _parse_vnet_id(remote_vnet_id)
    name = _peering_name(local_vnet, remote_vnet)

    nc = network_client(cred, local_sub)
    body = {
        "remote_virtual_network": {"id": remote_vnet_id},
        "allow_virtual_network_access": True,
        "allow_forwarded_traffic": False,
        "allow_gateway_transit": False,
        "use_remote_gateways": False,
    }
    try:
        poller = nc.virtual_network_peerings.begin_create_or_update(
            local_rg,
            local_vnet,
            name,
            body,
        )
        result = poller.result()
        state = getattr(result, "peering_state", None) or "Unknown"
        return name, str(state)
    except Exception as exc:
        if _is_idempotent_conflict(exc):
            LOGGER.info("vnet peering %s already exists (idempotent)", name)
            try:
                existing = nc.virtual_network_peerings.get(local_rg, local_vnet, name)
                state = getattr(existing, "peering_state", None) or "Connected"
                return name, str(state)
            except Exception:
                return name, "Connected"
        raise


def ensure_vnet_peering_with_cluster(
    cred: Any,
    *,
    subscription_id: str,
    cluster_resource_group: str,
    cluster_name: str,
    dashboard_vnet_id: str = "",
) -> dict[str, Any]:
    """Peer the dashboard platform VNet with the AKS-auto-created VNet.

    Without this peering, the api sidecar (running in the Container Apps
    Environment subnet of the platform VNet) cannot route traffic to the
    AKS internal-LoadBalancer Service IPs (in the AKS-auto VNet's
    ``10.224.0.0/12`` range). The symptom is an httpx 30 s timeout on
    ``/api/aks/openapi/proxy`` and ``/api/aks/openapi/spec`` even though
    the ``elb-openapi`` pods are healthy and the Service has endpoints.

    Best-effort by design:

    * ``dashboard_vnet_id`` empty AND ``PLATFORM_PRIVATE_ENDPOINT_SUBNET_ID``
      unset (local-dev shell) → returns ``{"skipped": True, "reason":
      "dashboard_vnet_id not resolved"}``.
    * AKS node-RG has no VNet (BYO-VNet mode — the cluster already lives
      in the platform VNet or operator-managed VNet) → returns
      ``{"skipped": True, "reason": "aks_node_rg_has_no_vnet"}``.
    * Both peering directions succeed (or already exist) → returns
      ``{"peerings": [...], "dashboard_vnet": ..., "aks_vnet": ...}``.
    * Either direction fails (permission denied, transient) → returns
      ``{"error": ..., "recovery_command": ...}``. The caller does NOT
      fail the AKS provision task.
    """

    dash_vnet = (dashboard_vnet_id or "").strip() or _dashboard_vnet_id_from_env()
    if not dash_vnet:
        return {
            "skipped": True,
            "reason": "dashboard_vnet_id not resolved",
            "recovery_command": _recovery_command(
                subscription_id=subscription_id,
                cluster_resource_group=cluster_resource_group,
                cluster_name=cluster_name,
            ),
        }

    # Read the cluster to find the node resource group (where AKS put its
    # auto-VNet) and to skip BYO-VNet mode cleanly.
    from api.services.azure_clients import aks_client

    aks_cl = aks_client(cred, subscription_id)
    try:
        cluster = aks_cl.managed_clusters.get(cluster_resource_group, cluster_name)
    except Exception as exc:
        return {
            "error": f"aks_client.managed_clusters.get failed: {type(exc).__name__}",
            "recovery_command": _recovery_command(
                subscription_id=subscription_id,
                cluster_resource_group=cluster_resource_group,
                cluster_name=cluster_name,
            ),
        }
    node_rg = (getattr(cluster, "node_resource_group", None) or "").strip()
    if not node_rg:
        return {
            "skipped": True,
            "reason": "cluster has no node_resource_group",
            "recovery_command": _recovery_command(
                subscription_id=subscription_id,
                cluster_resource_group=cluster_resource_group,
                cluster_name=cluster_name,
            ),
        }

    aks_vnet_id = _resolve_aks_node_vnet(
        cred,
        subscription_id=subscription_id,
        node_resource_group=node_rg,
    )
    if not aks_vnet_id:
        return {
            "skipped": True,
            "reason": "aks_node_rg_has_no_vnet",
            "dashboard_vnet": dash_vnet,
            "node_resource_group": node_rg,
            "recovery_command": _recovery_command(
                subscription_id=subscription_id,
                cluster_resource_group=cluster_resource_group,
                cluster_name=cluster_name,
            ),
        }

    peerings: list[dict[str, str]] = []
    error: str | None = None
    # dashboard → AKS
    try:
        name, state = _create_peering(
            cred,
            subscription_id=subscription_id,
            local_vnet_id=dash_vnet,
            remote_vnet_id=aks_vnet_id,
        )
        peerings.append({"direction": "dashboard_to_aks", "name": name, "state": state})
    except Exception as exc:
        LOGGER.warning("vnet peering dashboard→aks failed: %s", str(exc)[:200])
        error = f"dashboard_to_aks: {str(exc)[:300]}"

    # AKS → dashboard
    try:
        name, state = _create_peering(
            cred,
            subscription_id=subscription_id,
            local_vnet_id=aks_vnet_id,
            remote_vnet_id=dash_vnet,
        )
        peerings.append({"direction": "aks_to_dashboard", "name": name, "state": state})
    except Exception as exc:
        LOGGER.warning("vnet peering aks→dashboard failed: %s", str(exc)[:200])
        suffix = f"aks_to_dashboard: {str(exc)[:300]}"
        error = f"{error}; {suffix}" if error else suffix

    payload: dict[str, Any] = {
        "dashboard_vnet": dash_vnet,
        "aks_vnet": aks_vnet_id,
        "node_resource_group": node_rg,
        "peerings": peerings,
        "recovery_command": _recovery_command(
            subscription_id=subscription_id,
            cluster_resource_group=cluster_resource_group,
            cluster_name=cluster_name,
        ),
    }
    if error:
        payload["error"] = error
    return payload
