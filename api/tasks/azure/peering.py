"""Bidirectional VNet peering between the dashboard platform VNet and an AKS cluster VNet.

Responsibility: Stand up the two VNet peerings the api sidecar needs in order to reach an
    AKS internal-LoadBalancer Service IP from outside the AKS-managed VNet (the
    `elb-openapi` proxy / spec / Try-It flow). Idempotent — re-runs against an already
    peered pair are a silent no-op.
Edit boundaries: All VNet-peering writes belong here. Routes and the `provision_aks`
    orchestrator only call into `ensure_vnet_peering_with_cluster`; everything Azure SDK
    related stays in this module.
Key entry points: `ensure_vnet_peering_with_cluster`,
    `_dashboard_vnet_id_from_env`, `_recovery_command`, `probe_private_ip`.
Risky contracts: Treats `AlreadyExists` / `Conflict` as success. Any other failure is
    recorded into ``error`` and the caller (typically `provision_aks`) does **not**
    fail the task — the AKS cluster is fully usable; only OpenAPI proxy / spec / Try-It
    is unreachable until peering lands. The returned payload always includes a
    ``recovery_command`` string the SPA / operator can paste. When a target-VNet
    peering fails with an Azure RBAC denial (``AuthorizationFailed`` /
    ``LinkedAuthorizationFailed``) the payload additionally carries
    ``rbac_remediation`` (role + target-VNet scope + ready-to-paste
    ``az role assignment create``) because the generic ``recovery_command``
    only fixes platform-to-AKS peering, not target-to-AKS peering.
    `probe_private_ip` is the SSRF chokepoint: it refuses any non-RFC1918 / loopback /
    link-local / multicast target and any path with control characters so an
    authenticated caller cannot redirect the api sidecar's outbound HTTP at Azure
    IMDS (169.254.169.254) or other Container Apps Environment internal hosts.
Validation: `uv run pytest -q api/tests/test_azure_peering.py`.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import time
from typing import Any

import httpx

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


_NETWORK_CONTRIBUTOR_ROLE = "Network Contributor"
_OBJECT_ID_RE = re.compile(r"object id '([0-9a-fA-F-]{36})'")


def _is_authorization_failure(error: str | None) -> bool:
    """True when a peering error is an Azure RBAC denial (not a transient fault).

    Covers both ``AuthorizationFailed`` (the local-direction write is denied)
    and ``LinkedAuthorizationFailed`` (the local write is allowed but the
    linked remote VNet check is denied) — both mean the dashboard managed
    identity is missing peering-write on the *target* VNet.
    """
    if not error:
        return False
    low = error.lower()
    return "authorizationfailed" in low or "linkedauthorizationfailed" in low


def _mi_object_id_from_error(error: str | None) -> str:
    """Extract the managed identity object id Azure embeds in an authz error.

    Azure's ``AuthorizationFailed`` message always carries
    ``with object id '<guid>'``. Returns ``""`` when no id is present so the
    remediation falls back to a human-readable placeholder.
    """
    if not error:
        return ""
    match = _OBJECT_ID_RE.search(error)
    return match.group(1) if match else ""


def _rbac_remediation(*, target_vnet_id: str, mi_object_id: str) -> dict[str, str]:
    """Render the exact least-privilege grant that unblocks target peering.

    VNet peering requires write on BOTH ends. The dashboard MI already holds
    Contributor on the AKS-VNet resource group, so a single ``Network
    Contributor`` assignment scoped to the *target* VNet resolves both the
    ``target_to_aks`` (direct write) and ``aks_to_target`` (linked check)
    directions. Scoping to the VNet — not its resource group or the
    subscription — keeps the grant least-privilege per the hardening charter.
    """
    assignee = mi_object_id or "<dashboard-managed-identity-object-id>"
    command = (
        "az role assignment create "
        f"--assignee-object-id {assignee} "
        "--assignee-principal-type ServicePrincipal "
        f'--role "{_NETWORK_CONTRIBUTOR_ROLE}" '
        f"--scope {target_vnet_id}"
    )
    message = (
        "The dashboard managed identity lacks "
        "'Microsoft.Network/virtualNetworks/virtualNetworkPeerings/write' on the "
        "target VNet. Peering needs write on both ends, so grant the identity "
        "'Network Contributor' scoped to the target VNet (command below), then "
        "re-run 'Peer & probe'. Note: the 'peer-cluster-network.sh' recovery only "
        "fixes platform-to-AKS peering, not this target-to-AKS peering."
    )
    return {
        "role": _NETWORK_CONTRIBUTOR_ROLE,
        "scope": target_vnet_id,
        "command": command,
        "message": message,
    }


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


def _vnet_id_from_subnet_id(subnet_id: str) -> str:
    """Strip ``/subnets/<name>`` off a subnet ARM id to get the parent VNet id.

    Returns ``""`` when the id does not contain a ``/subnets/`` marker so a
    malformed value degrades to "not resolved" instead of raising.
    """
    sid = (subnet_id or "").strip()
    if not sid:
        return ""
    marker = "/subnets/"
    idx = sid.lower().find(marker)
    if idx < 0:
        return ""
    return sid[:idx]


def _resolve_aks_vnet_id(
    cred: Any,
    *,
    subscription_id: str,
    node_resource_group: str,
    cluster: Any,
) -> str:
    """Resolve the AKS cluster's VNet ARM id across managed-VNet and BYO-subnet modes.

    Two cluster topologies must both work:

    * **Managed-VNet mode** (``provision_aks`` default): AKS creates exactly one
      VNet inside the ``MC_*`` node resource group. ``_resolve_aks_node_vnet``
      finds it.
    * **BYO-subnet mode** (the dashboard's ``vnet-elb-dashboard/snet-aks``
      model): the agent pools reference an operator-managed subnet, the ``MC_*``
      node RG holds **no** VNet, and the cluster actually lives in the platform
      VNet. ``_resolve_aks_node_vnet`` returns ``""`` there — so fall back to the
      parent VNet of the first agent-pool ``vnet_subnet_id``.

    Returns ``""`` only when neither path resolves a VNet (genuinely
    unpeerable), letting the caller keep the existing ``aks_node_rg_has_no_vnet``
    skip semantics.
    """
    vnet_id = _resolve_aks_node_vnet(
        cred,
        subscription_id=subscription_id,
        node_resource_group=node_resource_group,
    )
    if vnet_id:
        return vnet_id

    # BYO-subnet fallback: derive the VNet from the agent pool subnet id.
    from api.services.aks.node_subnet_nsg import first_node_subnet_id

    subnet_id = first_node_subnet_id(cluster)
    byo_vnet_id = _vnet_id_from_subnet_id(subnet_id)
    if byo_vnet_id:
        LOGGER.info(
            "vnet peering: resolved AKS VNet from BYO node subnet (%s)",
            byo_vnet_id,
        )
    return byo_vnet_id


def _normalise_vnet_id(vnet_id: str) -> str:
    """Case-fold + strip trailing slash so two ARM ids compare equal."""
    return (vnet_id or "").rstrip("/").lower()


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


def _resolve_vnet_id(
    cred: Any,
    *,
    subscription_id: str,
    resource_group: str,
    vnet_name: str,
) -> str:
    rc = resource_client(cred, subscription_id)
    for resource in rc.resources.list_by_resource_group(
        resource_group,
        filter="resourceType eq 'Microsoft.Network/virtualNetworks'",
    ):
        resource_name = getattr(resource, "name", None) or ""
        resource_id = getattr(resource, "id", None) or ""
        if resource_name == vnet_name or resource_id.rstrip("/").endswith(
            f"/virtualNetworks/{vnet_name}"
        ):
            return str(resource_id)
    raise KeyError(
        f"virtual network '{vnet_name}' not found in resource group '{resource_group}'"
    )


def _validate_private_target(target_ip: str, target_path: str) -> tuple[bool, str, str]:
    """Refuse any IPv4 outside RFC1918 private space, plus any unsafe path.

    Returns ``(ok, normalised_path, message)``. When ``ok`` is False the
    caller must surface ``message`` and skip the probe — keeping the api
    sidecar from being weaponised as an SSRF gateway against IMDS
    (169.254.169.254), loopback services, public IPs, or random hosts in
    the Container Apps Environment VNet. IPv6 is rejected outright because
    (a) AKS auto-VNet is IPv4-only, (b) IPv4-mapped IPv6
    (``::ffff:169.254.169.254``) is an easy bypass vector, and (c) the
    plain ``http://{ip}{path}`` URL builder cannot express bracketed v6.
    """
    try:
        addr = ipaddress.IPv4Address(target_ip)
    except (ipaddress.AddressValueError, ValueError):
        return False, target_path, f"invalid target_ip (IPv4 required): {target_ip!r}"
    if (
        not addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    ):
        return (
            False,
            target_path,
            "target_ip must be an RFC1918 private IPv4 address "
            "(not loopback / link-local / multicast)",
        )
    path = (target_path or "/openapi.json").strip()
    if not path.startswith("/"):
        path = f"/{path}"
    if len(path) > 256:
        return False, path, "target_path too long (max 256 chars)"
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in path):
        return False, path, "target_path contains control characters"
    return True, path, ""


def probe_private_ip(
    *,
    target_ip: str,
    target_path: str = "/openapi.json",
    timeout: float = 2.0,
) -> dict[str, Any]:
    ok, path, reason = _validate_private_target(target_ip, target_path)
    if not ok:
        return {
            "target_ip": target_ip,
            "target_path": path,
            "url": "",
            "reachable": False,
            "status_code": None,
            "latency_ms": 0.0,
            "message": reason,
        }
    url = f"http://{target_ip}{path}"
    started = time.monotonic()
    try:
        response = httpx.get(url, timeout=timeout)
        elapsed_ms = round((time.monotonic() - started) * 1000, 1)
        return {
            "target_ip": target_ip,
            "target_path": path,
            "url": url,
            "reachable": response.is_success,
            "status_code": response.status_code,
            "latency_ms": elapsed_ms,
            "message": response.reason_phrase,
        }
    except httpx.HTTPError as exc:
        elapsed_ms = round((time.monotonic() - started) * 1000, 1)
        return {
            "target_ip": target_ip,
            "target_path": path,
            "url": url,
            "reachable": False,
            "status_code": None,
            "latency_ms": elapsed_ms,
            "message": str(exc),
        }


def _peer_vnets(
    cred: Any,
    *,
    local_vnet_id: str,
    remote_vnet_id: str,
    local_label: str,
    remote_label: str,
) -> dict[str, Any]:
    peerings: list[dict[str, str]] = []
    error_parts: list[str] = []

    try:
        name, state = _create_peering(
            cred,
            subscription_id="",
            local_vnet_id=local_vnet_id,
            remote_vnet_id=remote_vnet_id,
        )
        peerings.append({
            "direction": f"{local_label}_to_{remote_label}",
            "name": name,
            "state": state,
        })
    except Exception as exc:
        error_parts.append(f"{local_label}_to_{remote_label}: {str(exc)[:300]}")

    try:
        name, state = _create_peering(
            cred,
            subscription_id="",
            local_vnet_id=remote_vnet_id,
            remote_vnet_id=local_vnet_id,
        )
        peerings.append({
            "direction": f"{remote_label}_to_{local_label}",
            "name": name,
            "state": state,
        })
    except Exception as exc:
        error_parts.append(f"{remote_label}_to_{local_label}: {str(exc)[:300]}")

    return {
        "peerings": peerings,
        "error": "; ".join(error_parts) if error_parts else None,
    }


def ensure_vnet_peering_with_target(
    cred: Any,
    *,
    subscription_id: str,
    cluster_resource_group: str,
    cluster_name: str,
    target_subscription_id: str,
    target_resource_group: str,
    target_vnet_name: str,
    target_ip: str = "10.224.0.7",
    target_path: str = "/openapi.json",
) -> dict[str, Any]:
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
            "probe": probe_private_ip(target_ip=target_ip, target_path=target_path),
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
            "probe": probe_private_ip(target_ip=target_ip, target_path=target_path),
        }

    aks_vnet_id = _resolve_aks_vnet_id(
        cred,
        subscription_id=subscription_id,
        node_resource_group=node_rg,
        cluster=cluster,
    )
    if not aks_vnet_id:
        return {
            "skipped": True,
            "reason": "aks_node_rg_has_no_vnet",
            "message": (
                "Could not resolve the AKS cluster VNet: the MC_* node "
                "resource group has no VNet and no agent pool exposes a "
                "vnet_subnet_id. Nothing to peer."
            ),
            "node_resource_group": node_rg,
            "recovery_command": _recovery_command(
                subscription_id=subscription_id,
                cluster_resource_group=cluster_resource_group,
                cluster_name=cluster_name,
            ),
            "probe": probe_private_ip(target_ip=target_ip, target_path=target_path),
        }

    try:
        target_vnet_id = _resolve_vnet_id(
            cred,
            subscription_id=target_subscription_id,
            resource_group=target_resource_group,
            vnet_name=target_vnet_name,
        )
    except Exception as exc:
        return {
            "error": f"target_vnet lookup failed: {str(exc)[:200]}",
            "aks_vnet": aks_vnet_id,
            "node_resource_group": node_rg,
            "recovery_command": _recovery_command(
                subscription_id=subscription_id,
                cluster_resource_group=cluster_resource_group,
                cluster_name=cluster_name,
            ),
            "probe": probe_private_ip(target_ip=target_ip, target_path=target_path),
        }

    # In BYO-subnet mode the AKS "VNet" is the dashboard platform VNet. If the
    # operator picks that same VNet as the peering target, ARM rejects a
    # self-peering ("cannot peer a virtual network to itself"). Surface a clear
    # skip instead — VMs already in that VNet reach the OpenAPI IP directly.
    if _normalise_vnet_id(target_vnet_id) == _normalise_vnet_id(aks_vnet_id):
        return {
            "skipped": True,
            "reason": "target_vnet_is_aks_vnet",
            "message": (
                "The selected target VNet is the VNet the AKS cluster already "
                "runs in (BYO-subnet mode), so no peering is needed — VMs in "
                "this VNet reach the OpenAPI private IP directly."
            ),
            "aks_vnet": aks_vnet_id,
            "target_vnet": target_vnet_id,
            "node_resource_group": node_rg,
            "recovery_command": _recovery_command(
                subscription_id=subscription_id,
                cluster_resource_group=cluster_resource_group,
                cluster_name=cluster_name,
            ),
            "probe": probe_private_ip(target_ip=target_ip, target_path=target_path),
        }

    pair_summary = _peer_vnets(
        cred,
        local_vnet_id=target_vnet_id,
        remote_vnet_id=aks_vnet_id,
        local_label="target",
        remote_label="aks",
    )

    payload: dict[str, Any] = {
        "target_subscription_id": target_subscription_id,
        "target_resource_group": target_resource_group,
        "target_vnet_name": target_vnet_name,
        "target_vnet": target_vnet_id,
        "aks_vnet": aks_vnet_id,
        "node_resource_group": node_rg,
        "peerings": pair_summary["peerings"],
        "probe": probe_private_ip(target_ip=target_ip, target_path=target_path),
        "recovery_command": _recovery_command(
            subscription_id=subscription_id,
            cluster_resource_group=cluster_resource_group,
            cluster_name=cluster_name,
        ),
    }
    if pair_summary["error"]:
        payload["error"] = pair_summary["error"]
        if _is_authorization_failure(pair_summary["error"]):
            payload["rbac_remediation"] = _rbac_remediation(
                target_vnet_id=target_vnet_id,
                mi_object_id=_mi_object_id_from_error(pair_summary["error"]),
            )
    return payload


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

    aks_vnet_id = _resolve_aks_vnet_id(
        cred,
        subscription_id=subscription_id,
        node_resource_group=node_rg,
        cluster=cluster,
    )
    if not aks_vnet_id:
        return {
            "skipped": True,
            "reason": "aks_node_rg_has_no_vnet",
            "message": (
                "Could not resolve the AKS cluster VNet: the MC_* node "
                "resource group has no VNet and no agent pool exposes a "
                "vnet_subnet_id. Nothing to peer."
            ),
            "dashboard_vnet": dash_vnet,
            "node_resource_group": node_rg,
            "recovery_command": _recovery_command(
                subscription_id=subscription_id,
                cluster_resource_group=cluster_resource_group,
                cluster_name=cluster_name,
            ),
        }

    # BYO-subnet mode: the AKS cluster lives in the dashboard platform VNet, so
    # the AKS VNet *is* the dashboard VNet. There is nothing to peer (and ARM
    # rejects self-peering) — the api sidecar already shares the VNet and can
    # reach the OpenAPI internal-LB IP directly.
    if _normalise_vnet_id(aks_vnet_id) == _normalise_vnet_id(dash_vnet):
        return {
            "skipped": True,
            "reason": "aks_shares_dashboard_vnet",
            "dashboard_vnet": dash_vnet,
            "aks_vnet": aks_vnet_id,
            "node_resource_group": node_rg,
            "recovery_command": _recovery_command(
                subscription_id=subscription_id,
                cluster_resource_group=cluster_resource_group,
                cluster_name=cluster_name,
            ),
        }

    pair_summary = _peer_vnets(
        cred,
        local_vnet_id=dash_vnet,
        remote_vnet_id=aks_vnet_id,
        local_label="dashboard",
        remote_label="aks",
    )

    payload: dict[str, Any] = {
        "dashboard_vnet": dash_vnet,
        "aks_vnet": aks_vnet_id,
        "node_resource_group": node_rg,
        "peerings": pair_summary["peerings"],
        "recovery_command": _recovery_command(
            subscription_id=subscription_id,
            cluster_resource_group=cluster_resource_group,
            cluster_name=cluster_name,
        ),
    }
    if pair_summary["error"]:
        payload["error"] = pair_summary["error"]
    return payload
