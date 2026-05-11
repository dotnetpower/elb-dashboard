"""AKS cluster provision orchestrator.

Sequence:
  1. Create AKS cluster (OIDC issuer + Workload Identity enabled)
  2. Assign RBAC roles to kubelet identity (ACR pull, Storage)
  3. Set up Workload Identity for the OpenAPI pod
     a. Create User-Assigned Managed Identity
     b. Create Federated Credential (AKS OIDC ↔ K8s ServiceAccount)
     c. Assign Storage + AKS roles to the MI
  4. Deploy elb-openapi to AKS with the Workload Identity ServiceAccount

Output: dict with cluster info and assigned roles.
"""

from __future__ import annotations

import logging
from typing import Any

import azure.durable_functions as df

LOGGER = logging.getLogger(__name__)

# Name conventions for Workload Identity resources
MI_NAME = "id-elb-openapi"
K8S_SA_NAME = "elb-openapi-sa"
K8S_NAMESPACE = "default"
FED_CRED_NAME = "fc-elb-openapi"


def provision_aks_orchestrator(
    context: df.DurableOrchestrationContext,
) -> dict[str, Any]:
    request: dict[str, Any] = context.get_input() or {}
    cluster_name = request.get("cluster_name", "")

    # 1. Create AKS cluster (OIDC issuer + Workload Identity enabled)
    context.set_custom_status({"phase": "creating", "cluster_name": cluster_name})
    create_result = yield context.call_activity("create_aks_cluster_activity", request)

    # 2. Assign kubelet RBAC roles (ACR pull, Storage)
    context.set_custom_status({"phase": "assigning_roles", "cluster_name": cluster_name})
    try:
        role_result = yield context.call_activity("assign_aks_roles_activity", request)
        roles_assigned = role_result.get("roles_assigned", [])
    except Exception as exc:
        LOGGER.warning("Role assignment failed (non-fatal): %s", exc)
        roles_assigned = []

    # 3. Set up Workload Identity for OpenAPI pod
    context.set_custom_status({"phase": "setup_workload_identity", "cluster_name": cluster_name})
    wi_payload = {**request, "mi_name": MI_NAME, "k8s_sa_name": K8S_SA_NAME,
                  "k8s_namespace": K8S_NAMESPACE, "fed_cred_name": FED_CRED_NAME}
    try:
        wi_result = yield context.call_activity("setup_workload_identity_activity", wi_payload)
    except Exception as exc:
        LOGGER.warning("Workload identity setup failed (non-fatal): %s", exc)
        wi_result = {"error": str(exc)}

    # 4. Deploy elb-openapi to AKS
    context.set_custom_status({"phase": "deploying_openapi", "cluster_name": cluster_name})
    try:
        deploy_payload = {**request, "k8s_sa_name": K8S_SA_NAME,
                          "mi_client_id": wi_result.get("mi_client_id", "")}
        deploy_result = yield context.call_activity("deploy_openapi_activity", deploy_payload)
    except Exception as exc:
        LOGGER.warning("OpenAPI deployment failed (non-fatal): %s", exc)
        deploy_result = {"error": str(exc)}

    return {
        "cluster_name": cluster_name,
        "resource_group": request.get("resource_group"),
        "region": request.get("region"),
        "node_sku": request.get("node_sku", "Standard_E32s_v5"),
        "node_count": request.get("node_count", 10),
        "status": "succeeded",
        "roles_assigned": roles_assigned,
        "workload_identity": wi_result,
        "openapi_deploy": deploy_result,
    }
