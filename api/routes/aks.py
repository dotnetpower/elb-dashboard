"""AKS cluster lifecycle routes (``/api/aks/*``).

Provision/start/stop/delete clusters, deploy the elb-openapi service,
assign kubelet RBAC roles, and list allowed node SKUs.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import azure.durable_functions as df
import azure.functions as func

from _http_utils import (
    _RE_INSTANCE_ID,
    _RE_CLUSTER_NAME,
    _error_response,
    _json_response,
    _validate_name,
    _validate_rg,
    _validate_sub,
)
from auth.token import AuthError, validate_bearer_token
from services.azure_clients import credential_for_caller
from services.sanitise import sanitise

LOGGER = logging.getLogger(__name__)

bp = df.Blueprint()

# Allowed node SKUs for ElasticBLAST (E-series v5, memory-optimized)
_AKS_ALLOWED_SKUS = [
    "Standard_E16s_v5",
    "Standard_E20s_v5",
    "Standard_E32s_v5",
    "Standard_E48s_v5",
    "Standard_E64s_v5",
]


@bp.route(route="aks/skus", methods=["GET"])
def list_aks_skus(req: func.HttpRequest) -> func.HttpResponse:
    """Return the allowed node SKUs for AKS cluster provisioning."""
    try:
        validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)
    return _json_response({"skus": _AKS_ALLOWED_SKUS, "default": "Standard_E32s_v5"})


@bp.route(route="aks/provision", methods=["POST"])
@bp.durable_client_input(client_name="client")
async def provision_aks_cluster(
    req: func.HttpRequest, client: df.DurableOrchestrationClient
) -> func.HttpResponse:
    """Create an AKS cluster for ElasticBLAST. Returns immediately — polls via status endpoint."""
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)
    raw = req.get_body()
    if not raw:
        return _error_response(400, "request body required")
    try:
        body = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        return _error_response(400, f"invalid JSON: {exc}")

    required = {"subscription_id", "resource_group", "region", "cluster_name"}
    missing = required - body.keys()
    if missing:
        return _error_response(400, f"missing fields: {sorted(missing)}")

    sub = body["subscription_id"]
    rg = body["resource_group"]
    cluster_name = body["cluster_name"]
    node_sku = body.get("node_sku", "Standard_E32s_v5")
    node_count = body.get("node_count", 10)

    if err := _validate_sub(sub):
        return _error_response(400, err)
    if err := _validate_rg(rg):
        return _error_response(400, err)
    if err := _validate_name(cluster_name, _RE_CLUSTER_NAME, "cluster_name"):
        return _error_response(400, err)
    if node_sku not in _AKS_ALLOWED_SKUS:
        return _error_response(400, f"node_sku must be one of: {', '.join(_AKS_ALLOWED_SKUS)}")
    if not isinstance(node_count, int) or node_count < 3 or node_count > 20:
        return _error_response(400, "node_count must be between 3 and 20")

    orchestration_input = {
        **body,
        "user_assertion": identity.raw_token,
        "owner_oid": identity.object_id,
    }
    instance_id = await client.start_new(
        "provision_aks_orchestrator", None, orchestration_input
    )
    LOGGER.info("started provision_aks_orchestrator instance=%s cluster=%s", instance_id, cluster_name)
    return client.create_check_status_response(req, instance_id)


@bp.route(route="aks/openapi/deploy", methods=["POST"])
@bp.durable_client_input(client_name="client")
async def deploy_openapi(
    req: func.HttpRequest, client: df.DurableOrchestrationClient
) -> func.HttpResponse:
    """Re-deploy the OpenAPI service to an existing AKS cluster."""
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)
    raw = req.get_body()
    if not raw:
        return _error_response(400, "request body required")
    try:
        body = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        return _error_response(400, f"invalid JSON: {exc}")

    required = {"subscription_id", "resource_group", "cluster_name"}
    missing = required - body.keys()
    if missing:
        return _error_response(400, f"missing fields: {sorted(missing)}")
    if err := _validate_sub(body["subscription_id"]):
        return _error_response(400, err)
    if err := _validate_rg(body["resource_group"]):
        return _error_response(400, err)
    if err := _validate_name(body["cluster_name"], _RE_CLUSTER_NAME, "cluster_name"):
        return _error_response(400, err)

    orchestration_input = {
        **body,
        "user_assertion": identity.raw_token,
        "owner_oid": identity.object_id,
    }
    instance_id = await client.start_new(
        "deploy_openapi_orchestrator", None, orchestration_input
    )
    LOGGER.info(
        "started deploy_openapi_orchestrator instance=%s cluster=%s",
        instance_id, body["cluster_name"],
    )
    return client.create_check_status_response(req, instance_id)


@bp.route(route="aks/openapi/deploy/{instance_id}/status", methods=["GET"])
@bp.durable_client_input(client_name="client")
async def get_deploy_openapi_status(
    req: func.HttpRequest, client: df.DurableOrchestrationClient
) -> func.HttpResponse:
    """Poll the OpenAPI deployment orchestrator status."""
    try:
        validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)

    instance_id = req.route_params.get("instance_id")
    if not instance_id or not _RE_INSTANCE_ID.match(instance_id):
        return _error_response(400, "invalid instance_id")
    status = await client.get_status(instance_id, show_input=False)
    if status is None or status.runtime_status is None:
        return _error_response(404, "instance not found")
    return _json_response(
        {
            "instance_id": status.instance_id,
            "runtime_status": status.runtime_status.name,
            "custom_status": status.custom_status,
            "created_time": status.created_time,
            "last_updated_time": status.last_updated_time,
            "output": status.output,
        }
    )


@bp.route(route="aks/delete", methods=["POST"])
def delete_aks_cluster(req: func.HttpRequest) -> func.HttpResponse:
    """Delete an AKS cluster."""
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)
    raw = req.get_body()
    if not raw:
        return _error_response(400, "request body required")
    try:
        body = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        return _error_response(400, f"invalid JSON: {exc}")

    required = {"subscription_id", "resource_group", "cluster_name"}
    missing = required - body.keys()
    if missing:
        return _error_response(400, f"missing fields: {sorted(missing)}")

    if err := _validate_sub(body["subscription_id"]):
        return _error_response(400, err)
    if err := _validate_rg(body["resource_group"]):
        return _error_response(400, err)
    if err := _validate_name(body["cluster_name"], _RE_CLUSTER_NAME, "cluster_name"):
        return _error_response(400, err)

    cred = credential_for_caller(identity.raw_token)
    try:
        from azure.mgmt.containerservice import ContainerServiceClient
        aks_client = ContainerServiceClient(cred, body["subscription_id"])
        aks_client.managed_clusters.begin_delete(body["resource_group"], body["cluster_name"])
        LOGGER.info("AKS cluster delete started: %s in %s", body["cluster_name"], body["resource_group"])
        return _json_response({"cluster_name": body["cluster_name"], "status": "deleting"})
    except Exception as exc:
        LOGGER.warning("AKS delete failed: %s", exc)
        return _error_response(500, sanitise(str(exc)))


@bp.route(route="aks/start", methods=["POST"])
def start_aks_cluster(req: func.HttpRequest) -> func.HttpResponse:
    """Start a stopped AKS cluster."""
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)
    raw = req.get_body()
    if not raw:
        return _error_response(400, "request body required")
    body = json.loads(raw.decode("utf-8"))
    required = {"subscription_id", "resource_group", "cluster_name"}
    missing = required - body.keys()
    if missing:
        return _error_response(400, f"missing fields: {sorted(missing)}")
    if err := _validate_sub(body["subscription_id"]):
        return _error_response(400, err)
    if err := _validate_rg(body["resource_group"]):
        return _error_response(400, err)
    if err := _validate_name(body["cluster_name"], _RE_CLUSTER_NAME, "cluster_name"):
        return _error_response(400, err)
    cred = credential_for_caller(identity.raw_token)
    try:
        from azure.mgmt.containerservice import ContainerServiceClient
        aks_client = ContainerServiceClient(cred, body["subscription_id"])
        aks_client.managed_clusters.begin_start(body["resource_group"], body["cluster_name"])
        LOGGER.info("AKS cluster start initiated: %s", body["cluster_name"])
        return _json_response({"cluster_name": body["cluster_name"], "status": "starting"})
    except Exception as exc:
        LOGGER.warning("AKS start failed: %s", exc)
        return _error_response(500, sanitise(str(exc)))


@bp.route(route="aks/stop", methods=["POST"])
def stop_aks_cluster(req: func.HttpRequest) -> func.HttpResponse:
    """Stop a running AKS cluster to save cost."""
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)
    raw = req.get_body()
    if not raw:
        return _error_response(400, "request body required")
    body = json.loads(raw.decode("utf-8"))
    required = {"subscription_id", "resource_group", "cluster_name"}
    missing = required - body.keys()
    if missing:
        return _error_response(400, f"missing fields: {sorted(missing)}")
    if err := _validate_sub(body["subscription_id"]):
        return _error_response(400, err)
    if err := _validate_rg(body["resource_group"]):
        return _error_response(400, err)
    if err := _validate_name(body["cluster_name"], _RE_CLUSTER_NAME, "cluster_name"):
        return _error_response(400, err)
    cred = credential_for_caller(identity.raw_token)
    try:
        from azure.mgmt.containerservice import ContainerServiceClient
        aks_client = ContainerServiceClient(cred, body["subscription_id"])
        aks_client.managed_clusters.begin_stop(body["resource_group"], body["cluster_name"])
        LOGGER.info("AKS cluster stop initiated: %s", body["cluster_name"])
        return _json_response({"cluster_name": body["cluster_name"], "status": "stopping"})
    except Exception as exc:
        LOGGER.warning("AKS stop failed: %s", exc)
        return _error_response(500, sanitise(str(exc)))


@bp.route(route="aks/{cluster_name}/assign-roles", methods=["POST"])
def assign_aks_roles(req: func.HttpRequest) -> func.HttpResponse:
    """Assign RBAC roles to AKS kubelet identity for ACR pull and storage access."""
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)
    raw = req.get_body()
    if not raw:
        return _error_response(400, "request body required")
    try:
        body = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        return _error_response(400, f"invalid JSON: {exc}")

    sub = body.get("subscription_id", "")
    rg = body.get("resource_group", "")
    cluster_name = req.route_params.get("cluster_name", "")
    acr_rg = body.get("acr_resource_group", "")
    acr_name = body.get("acr_name", "")
    storage_rg = body.get("storage_resource_group", "")
    storage_account = body.get("storage_account", "")

    if not all([sub, rg, cluster_name]):
        return _error_response(400, "subscription_id, resource_group required")

    cred = credential_for_caller(identity.raw_token)
    assigned = []
    try:
        from azure.mgmt.containerservice import ContainerServiceClient
        from azure.mgmt.authorization import AuthorizationManagementClient

        aks_client = ContainerServiceClient(cred, sub)
        cluster = aks_client.managed_clusters.get(rg, cluster_name)
        kubelet_oid = None
        if cluster.identity_profile and "kubeletidentity" in cluster.identity_profile:
            kubelet_oid = cluster.identity_profile["kubeletidentity"].object_id

        if not kubelet_oid:
            return _error_response(400, "kubelet identity not found on cluster")

        auth_client = AuthorizationManagementClient(cred, sub)

        if acr_rg and acr_name:
            scope = f"/subscriptions/{sub}/resourceGroups/{acr_rg}/providers/Microsoft.ContainerRegistry/registries/{acr_name}"
            if _assign_role(auth_client, scope, kubelet_oid, "7f951dda-4ed3-4680-a7ca-43fe172d538d"):
                assigned.append("AcrPull")

        if storage_rg and storage_account:
            scope = f"/subscriptions/{sub}/resourceGroups/{storage_rg}/providers/Microsoft.Storage/storageAccounts/{storage_account}"
            if _assign_role(auth_client, scope, kubelet_oid, "ba92f5b4-2d11-453d-a403-e96b0029c9fe"):
                assigned.append("StorageBlobDataContributor")

        return _json_response({"kubelet_oid": kubelet_oid, "roles_assigned": assigned})
    except Exception as exc:
        LOGGER.warning("Role assignment failed: %s", exc)
        return _error_response(500, sanitise(str(exc)))


def _assign_role(auth_client: Any, scope: str, principal_id: str, role_definition_id: str) -> bool:
    """Assign a role to a principal. Idempotent — soft-fails on permission errors.

    The Function App MI usually has only Contributor at subscription scope,
    which does NOT include `Microsoft.Authorization/roleAssignments/write`.
    On `AuthorizationFailed` / `InsufficientPermissions` we log the exact
    `az role assignment create` an admin can run, but do NOT raise — callers
    treat this as best-effort. Returns True when the role was created or
    already existed, False when permissions prevented assignment. Hard failures
    (network, throttling) bubble up.
    """
    role_def = f"{scope}/providers/Microsoft.Authorization/roleDefinitions/{role_definition_id}"
    assignment_name = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{scope}:{principal_id}:{role_definition_id}"))
    try:
        auth_client.role_assignments.create(
            scope, assignment_name,
            {
                "role_definition_id": role_def,
                "principal_id": principal_id,
                "principal_type": "ServicePrincipal",
            },
        )
        return True
    except Exception as exc:
        msg = str(exc)
        if "Conflict" in msg or "RoleAssignmentExists" in msg:
            LOGGER.debug("Role already assigned, skipping: %s", assignment_name)
            return True
        if "AuthorizationFailed" in msg or "InsufficientPermissions" in msg or "does not have authorization" in msg:
            LOGGER.warning(
                "Cannot self-grant role %s to principal %s on %s. "
                "Run as admin: az role assignment create --assignee-object-id %s "
                "--assignee-principal-type ServicePrincipal --role %s --scope '%s'",
                role_definition_id, principal_id, scope,
                principal_id, role_definition_id, scope,
            )
            return False
        raise


@bp.route(route="aks/openapi/spec", methods=["GET"])
def proxy_openapi_spec(req: func.HttpRequest) -> func.HttpResponse:
    """Proxy the openapi.json from the AKS-hosted elb-openapi service.

    The SWA's CSP blocks direct fetch to the AKS LoadBalancer IP (http://,
    not in connect-src). This route fetches the spec server-side and returns
    it, so the browser only talks to the same-origin /api/ endpoint.
    """
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)

    sub = req.params.get("subscription_id", "")
    rg = req.params.get("resource_group", "")
    cluster_name = req.params.get("cluster_name", "")
    if not all([sub, rg, cluster_name]):
        return _error_response(400, "subscription_id, resource_group, cluster_name required")

    cred = credential_for_caller(identity.raw_token)

    # 1. Discover the service external IP
    try:
        from services import monitoring as monitoring_svc

        external_ip = monitoring_svc.k8s_get_service_ip(
            cred, sub, rg, cluster_name, "elb-openapi",
        )
        if not external_ip:
            return _error_response(404, "elb-openapi service has no external IP yet")
    except Exception as exc:
        return _error_response(404, f"elb-openapi service not found: {sanitise(str(exc)[:200])}")

    # 2. Fetch openapi.json from the service
    import requests as _requests

    try:
        resp = _requests.get(f"http://{external_ip}/openapi.json", timeout=10)
        resp.raise_for_status()
    except Exception as exc:
        return _error_response(502, f"Failed to fetch openapi.json from {external_ip}: {sanitise(str(exc)[:200])}")

    return func.HttpResponse(
        body=resp.text,
        status_code=200,
        mimetype="application/json",
        headers={"Cache-Control": "no-cache"},
    )


@bp.route(route="aks/openapi/proxy", methods=["GET", "POST", "DELETE"])
def proxy_openapi_request(req: func.HttpRequest) -> func.HttpResponse:
    """Proxy arbitrary requests to the AKS-hosted elb-openapi service.

    The SWA is served over HTTPS, but the AKS LoadBalancer exposes HTTP only.
    Browsers block mixed-content requests, so we relay them server-side.

    Query params:
      subscription_id, resource_group, cluster_name — to discover the service IP.
      path — the path on the target service (e.g. /healthz, /v1/health).
    """
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)

    sub = req.params.get("subscription_id", "")
    rg = req.params.get("resource_group", "")
    cluster_name = req.params.get("cluster_name", "")
    target_path = req.params.get("path", "/")
    if not all([sub, rg, cluster_name]):
        return _error_response(400, "subscription_id, resource_group, cluster_name required")

    # Validate target_path to prevent SSRF — must start with /
    if not target_path.startswith("/"):
        target_path = "/" + target_path

    cred = credential_for_caller(identity.raw_token)

    # 1. Discover the service external IP
    try:
        from services import monitoring as monitoring_svc

        external_ip = monitoring_svc.k8s_get_service_ip(
            cred, sub, rg, cluster_name, "elb-openapi",
        )
        if not external_ip:
            return _error_response(404, "elb-openapi service has no external IP yet")
    except Exception as exc:
        return _error_response(404, f"elb-openapi service not found: {sanitise(str(exc)[:200])}")

    # 2. Forward the request
    import requests as _requests

    target_url = f"http://{external_ip}{target_path}"
    method = req.method.upper()
    headers = {"Content-Type": "application/json"}
    body = None
    if method in ("POST", "PUT", "PATCH"):
        body = req.get_body() or None

    try:
        resp = _requests.request(
            method, target_url, headers=headers, data=body, timeout=30,
        )
    except Exception as exc:
        return _error_response(502, f"Proxy request failed: {sanitise(str(exc)[:200])}")

    return func.HttpResponse(
        body=resp.text,
        status_code=resp.status_code,
        mimetype=resp.headers.get("content-type", "application/json"),
        headers={"Cache-Control": "no-cache"},
    )
