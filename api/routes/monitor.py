"""Read-only monitoring endpoints exposed under ``/api/monitor/*``.

Covers AKS (clusters, run-command, service IP, nodes, pods, top-nodes,
pod logs), Storage account (summary, public access toggle + bounded TTL
window orchestrator), ACR repository listing, and Remote Terminal VM
status. All endpoints require a valid bearer token and use the Function
App Managed Identity for downstream Azure SDK calls.
"""

from __future__ import annotations

import json
import logging

import azure.durable_functions as df
import azure.functions as func

from _http_utils import (
    _RE_CLUSTER_NAME,
    _RE_DB_NAME,
    _RE_INSTANCE_ID,
    _RE_STORAGE_ACCOUNT,
    _error_response,
    _json_response,
    _require_query,
    _validate_name,
    _validate_rg,
    _validate_sub,
)
from auth.token import AuthError, validate_bearer_token
from services import monitoring as monitoring_svc
from services.azure_clients import credential_for_caller
from services.sanitise import sanitise

LOGGER = logging.getLogger(__name__)

bp = df.Blueprint()


@bp.route(route="monitor/aks", methods=["GET"])
def monitor_aks(req: func.HttpRequest) -> func.HttpResponse:
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)
    params, err = _require_query(req, "subscription_id", "resource_group")
    if err:
        return err
    cred = credential_for_caller(identity.raw_token)
    clusters = monitoring_svc.list_aks_clusters(
        cred, params["subscription_id"], params["resource_group"]
    )
    return _json_response({"clusters": clusters})


@bp.route(route="monitor/aks/run-command", methods=["POST"])
def aks_run_command(req: func.HttpRequest) -> func.HttpResponse:
    """Execute a read-only kubectl command on an AKS cluster via Run Command API."""
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)
    raw = req.get_body()
    if not raw:
        return _error_response(400, "request body required")
    body = json.loads(raw.decode("utf-8"))
    sub = body.get("subscription_id", "")
    rg = body.get("resource_group", "")
    cluster = body.get("cluster_name", "")
    command = body.get("command", "")
    if not all([sub, rg, cluster, command]):
        return _error_response(400, "subscription_id, resource_group, cluster_name, command required")
    if err := _validate_sub(sub):
        return _error_response(400, err)
    if err := _validate_rg(rg):
        return _error_response(400, err)
    cred = credential_for_caller(identity.raw_token)
    try:
        result = monitoring_svc.run_aks_command(cred, sub, rg, cluster, command)
        return _json_response(result)
    except ValueError as exc:
        return _error_response(400, str(exc))
    except Exception as exc:
        return _error_response(500, sanitise(str(exc)))


# ---------------------------------------------------------------------------
# AKS — Direct Kubernetes API (fast, ~1-3s instead of ~30s)
# ---------------------------------------------------------------------------

@bp.route(route="monitor/aks/service-ip", methods=["GET"])
def aks_get_service_ip(req: func.HttpRequest) -> func.HttpResponse:
    """Get the external IP of a K8s LoadBalancer service."""
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)
    params, err = _require_query(req, "subscription_id", "resource_group", "cluster_name", "service_name")
    if err:
        return err
    cred = credential_for_caller(identity.raw_token)
    try:
        ip = monitoring_svc.k8s_get_service_ip(
            cred, params["subscription_id"], params["resource_group"],
            params["cluster_name"], params["service_name"],
            namespace=req.params.get("namespace", "default"),
        )
        if ip:
            return _json_response({"service_name": params["service_name"], "external_ip": ip})
        return _error_response(404, f"Service {params['service_name']} not found or has no external IP")
    except Exception as exc:
        return _error_response(500, sanitise(str(exc)))


# ---------------------------------------------------------------------------
# AKS — Warmup status (DB cache on nodes)
# ---------------------------------------------------------------------------

@bp.route(route="monitor/aks/warmup-status", methods=["GET"])
def aks_warmup_status(req: func.HttpRequest) -> func.HttpResponse:
    """Check warmup state: which DBs are loaded on AKS nodes."""
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)
    params, err = _require_query(req, "subscription_id", "resource_group", "cluster_name")
    if err:
        return err
    cred = credential_for_caller(identity.raw_token)
    try:
        status = monitoring_svc.k8s_warmup_status(
            cred, params["subscription_id"], params["resource_group"],
            params["cluster_name"],
        )
        return _json_response(status)
    except Exception as exc:
        return _error_response(500, sanitise(str(exc)))


@bp.route(route="warmup/start", methods=["POST"])
@bp.durable_client_input(client_name="client")
async def start_warmup(
    req: func.HttpRequest, client: df.DurableOrchestrationClient
) -> func.HttpResponse:
    """Start standalone DB warmup on an AKS cluster."""
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)
    raw = req.get_body()
    if not raw:
        return _error_response(400, "request body required")
    try:
        body = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        return _error_response(400, "invalid JSON")

    # Validate required fields with regex
    sub = body.get("subscription_id", "")
    rg = body.get("resource_group", "")
    storage_account = body.get("storage_account", "")
    db = body.get("db", "")
    cluster_name = body.get("aks_cluster_name", "")
    if err := _validate_sub(sub):
        return _error_response(400, err)
    if err := _validate_rg(rg):
        return _error_response(400, err)
    if err := _validate_name(storage_account, _RE_STORAGE_ACCOUNT, "storage_account"):
        return _error_response(400, err)
    if err := _validate_name(cluster_name, _RE_CLUSTER_NAME, "aks_cluster_name"):
        return _error_response(400, err)
    if not db:
        return _error_response(400, "db is required")
    # db is a path like "blast-db/core_nt" or a full URL — reject shell metacharacters
    import re as _re
    if _re.search(r"[;&|`$(){}\\!\n\r<>~\[\]?*]", db):
        return _error_response(400, "db contains invalid characters")

    # Whitelist only known fields — never pass raw client body to orchestrator
    storage_rg = body.get("storage_resource_group", rg)
    if err := _validate_rg(storage_rg):
        return _error_response(400, f"storage_resource_group: {err}")
    safe_input = {
        "subscription_id": sub,
        "resource_group": rg,
        "storage_account": storage_account,
        "storage_resource_group": storage_rg,
        "region": body.get("region", "koreacentral"),
        "db": db,
        "db_display_name": body.get("db_display_name", db),
        "program": body.get("program", "blastn") if body.get("program") in (
            "blastn", "blastp", "blastx", "tblastn", "tblastx", None, ""
        ) else "blastn",
        "aks_cluster_name": cluster_name,
        "machine_type": body.get("machine_type", ""),
        "num_nodes": body.get("num_nodes"),
        "acr_resource_group": body.get("acr_resource_group", ""),
        "acr_name": body.get("acr_name", ""),
        "terminal_resource_group": body.get("terminal_resource_group", ""),
        "terminal_vm_name": body.get("terminal_vm_name", ""),
        "user_assertion": identity.raw_token,
        "owner_oid": identity.object_id,
    }

    instance_id = await client.start_new("warmup_db_orchestrator", None, safe_input)
    LOGGER.info("Started warmup_db_orchestrator db=%s cluster=%s instance=%s",
                sanitise(db), sanitise(cluster_name), instance_id)
    return _json_response({"instance_id": instance_id, "db": safe_input["db_display_name"]}, status=202)


@bp.route(route="warmup/{instance_id}/status", methods=["GET"])
@bp.durable_client_input(client_name="client")
async def get_warmup_status(
    req: func.HttpRequest, client: df.DurableOrchestrationClient
) -> func.HttpResponse:
    """Poll warmup orchestrator status."""
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
    return _json_response({
        "instance_id": status.instance_id,
        "runtime_status": status.runtime_status.name,
        "custom_status": status.custom_status,
        "output": status.output,
    })


@bp.route(route="monitor/aks/nodes", methods=["GET"])
def aks_get_nodes(req: func.HttpRequest) -> func.HttpResponse:
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)
    params, err = _require_query(req, "subscription_id", "resource_group", "cluster_name")
    if err:
        return err
    cred = credential_for_caller(identity.raw_token)
    try:
        nodes = monitoring_svc.k8s_get_nodes(cred, params["subscription_id"], params["resource_group"], params["cluster_name"])
        return _json_response({"nodes": nodes})
    except Exception as exc:
        return _error_response(500, sanitise(str(exc)))


@bp.route(route="monitor/aks/pods", methods=["GET"])
def aks_get_pods(req: func.HttpRequest) -> func.HttpResponse:
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)
    params, err = _require_query(req, "subscription_id", "resource_group", "cluster_name")
    if err:
        return err
    cred = credential_for_caller(identity.raw_token)
    try:
        pods = monitoring_svc.k8s_get_pods(cred, params["subscription_id"], params["resource_group"], params["cluster_name"])
        return _json_response({"pods": pods})
    except Exception as exc:
        return _error_response(500, sanitise(str(exc)))


@bp.route(route="monitor/aks/top-nodes", methods=["GET"])
def aks_top_nodes(req: func.HttpRequest) -> func.HttpResponse:
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)
    params, err = _require_query(req, "subscription_id", "resource_group", "cluster_name")
    if err:
        return err
    cred = credential_for_caller(identity.raw_token)
    try:
        metrics = monitoring_svc.k8s_top_nodes(cred, params["subscription_id"], params["resource_group"], params["cluster_name"])
        return _json_response({"nodes": metrics})
    except Exception as exc:
        return _error_response(500, sanitise(str(exc)))


@bp.route(route="monitor/aks/pod-logs", methods=["GET"])
def aks_pod_logs(req: func.HttpRequest) -> func.HttpResponse:
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)
    params, err = _require_query(req, "subscription_id", "resource_group", "cluster_name", "namespace", "pod_name")
    if err:
        return err
    try:
        tail = int(req.params.get("tail", "200"))
    except ValueError:
        return _error_response(400, "tail must be a valid integer")
    if tail < 1 or tail > 10000:
        return _error_response(400, "tail must be between 1 and 10000")
    cred = credential_for_caller(identity.raw_token)
    try:
        logs = monitoring_svc.k8s_pod_logs(cred, params["subscription_id"], params["resource_group"], params["cluster_name"], params["namespace"], params["pod_name"], tail)
        return _json_response({"logs": logs, "pod_name": params["pod_name"], "namespace": params["namespace"]})
    except Exception as exc:
        return _error_response(500, sanitise(str(exc)))


@bp.route(route="monitor/storage", methods=["GET"])
def monitor_storage(req: func.HttpRequest) -> func.HttpResponse:
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)
    params, err = _require_query(req, "subscription_id", "resource_group", "account_name")
    if err:
        return err
    cred = credential_for_caller(identity.raw_token)
    summary = monitoring_svc.get_storage_summary(
        cred, params["subscription_id"], params["resource_group"], params["account_name"]
    )
    return _json_response(summary)


@bp.route(route="monitor/storage/public-access", methods=["POST"])
def toggle_storage_public_access(req: func.HttpRequest) -> func.HttpResponse:
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)
    try:
        body = json.loads(req.get_body() or b"{}")
    except json.JSONDecodeError as exc:
        return _error_response(400, f"invalid JSON: {exc}")
    required = {"subscription_id", "resource_group", "account_name", "enabled"}
    missing = required - body.keys()
    if missing:
        return _error_response(400, f"missing fields: {sorted(missing)}")
    cred = credential_for_caller(identity.raw_token)
    result = monitoring_svc.set_storage_public_access(
        cred,
        body["subscription_id"],
        body["resource_group"],
        body["account_name"],
        bool(body["enabled"]),
    )
    LOGGER.info(
        "storage public-access toggled by oid=%s account=%s -> %s",
        identity.object_id,
        body["account_name"],
        result.get("public_network_access"),
    )
    return _json_response(result)


@bp.route(route="monitor/storage/public-access/window", methods=["POST"])
@bp.durable_client_input(client_name="client")
async def start_storage_public_access_window(
    req: func.HttpRequest, client: df.DurableOrchestrationClient
) -> func.HttpResponse:
    """Enable public access for a bounded TTL, then auto-disable.

    Request body: subscription_id, resource_group, account_name, ttl_seconds (optional).
    """
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)
    try:
        body = json.loads(req.get_body() or b"{}")
    except json.JSONDecodeError as exc:
        return _error_response(400, f"invalid JSON: {exc}")
    required = {"subscription_id", "resource_group", "account_name"}
    missing = required - body.keys()
    if missing:
        return _error_response(400, f"missing fields: {sorted(missing)}")
    payload = {
        **body,
        "user_assertion": identity.raw_token,
        "owner_oid": identity.object_id,
    }
    instance_id = await client.start_new("storage_public_access_window_orchestrator", None, payload)
    return client.create_check_status_response(req, instance_id)


@bp.route(route="monitor/acr", methods=["GET"])
def monitor_acr(req: func.HttpRequest) -> func.HttpResponse:
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)
    params, err = _require_query(req, "subscription_id", "resource_group", "registry_name")
    if err:
        return err
    cred = credential_for_caller(identity.raw_token)
    summary = monitoring_svc.list_acr_repositories(
        cred, params["subscription_id"], params["resource_group"], params["registry_name"]
    )
    return _json_response(summary)


@bp.route(route="monitor/terminal", methods=["GET"])
def monitor_terminal(req: func.HttpRequest) -> func.HttpResponse:
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)
    params, err = _require_query(req, "subscription_id", "resource_group", "vm_name")
    if err:
        return err
    cred = credential_for_caller(identity.raw_token)
    try:
        status = monitoring_svc.get_vm_status(
            cred, params["subscription_id"], params["resource_group"], params["vm_name"]
        )
    except Exception as exc:
        exc_type = type(exc).__name__
        if exc_type == "ResourceNotFoundError" or "not found" in str(exc).lower():
            return _json_response(
                {"error": "VM not found", "vm_name": params["vm_name"], "resource_group": params["resource_group"]},
                status=404,
            )
        LOGGER.warning("monitor_terminal unexpected error: %s", exc_type)
        return _error_response(500, "Failed to query VM status")
    return _json_response(status)
