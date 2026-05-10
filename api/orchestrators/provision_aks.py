"""AKS cluster provision orchestrator.

Sequence:
  1. Create AKS cluster (5-10 min)
  2. Assign RBAC roles to kubelet identity

Output: dict with cluster info and assigned roles.
"""

from __future__ import annotations

import logging
from typing import Any

import azure.durable_functions as df

LOGGER = logging.getLogger(__name__)


def provision_aks_orchestrator(
    context: df.DurableOrchestrationContext,
) -> dict[str, Any]:
    request: dict[str, Any] = context.get_input() or {}
    cluster_name = request.get("cluster_name", "")

    # 1. Create AKS cluster
    context.set_custom_status({"phase": "creating", "cluster_name": cluster_name})
    create_result = yield context.call_activity("create_aks_cluster_activity", request)

    # 2. Assign roles
    context.set_custom_status({"phase": "assigning_roles", "cluster_name": cluster_name})
    try:
        role_result = yield context.call_activity("assign_aks_roles_activity", request)
        roles_assigned = role_result.get("roles_assigned", [])
    except Exception as exc:
        LOGGER.warning("Role assignment failed (non-fatal): %s", exc)
        roles_assigned = []

    return {
        "cluster_name": cluster_name,
        "resource_group": request.get("resource_group"),
        "region": request.get("region"),
        "node_sku": request.get("node_sku", "Standard_E32s_v5"),
        "node_count": request.get("node_count", 10),
        "status": "succeeded",
        "roles_assigned": roles_assigned,
    }
