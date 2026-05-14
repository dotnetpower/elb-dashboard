"""Monitor endpoints — AKS / Storage / ACR / Terminal / Jobs.

Reuses the legacy `services.monitoring` module from `api/services/`
(re-exported via `api_app.services`). The api sidecar uses the Container App's
shared user-assigned managed identity for all Azure SDK calls.

ERROR POLICY
------------
Monitor endpoints are READ-ONLY dashboard sources. They must NEVER 500 on
the SPA — a missing or RBAC-denied resource simply means "no data". This
file translates `HttpResponseError`/`AuthorizationFailed`/`NotFound` into
empty payloads with a `degraded_reason` field so the SPA can render an
informative empty state instead of crashing on `o.map is not a function`.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from azure.core.exceptions import (
    AzureError,
    HttpResponseError,
    ResourceNotFoundError,
)
from fastapi import APIRouter, Depends, HTTPException, Query

from api_app.auth import CallerIdentity, require_caller
from api_app.services import get_credential

# Bridge legacy services package
import api_app.services as _bootstrap  # noqa: F401

from services import monitoring as monitoring_svc  # type: ignore  # noqa: E402
from services.sanitise import sanitise  # type: ignore  # noqa: E402

LOGGER = logging.getLogger(__name__)

router = APIRouter(tags=["monitor"])


def _sub_default() -> str:
    return os.environ.get("AZURE_SUBSCRIPTION_ID", "")


def _graceful(op: str, exc: Exception, *, empty: Any) -> Any:
    """Translate a downstream exception into a degraded-but-valid response.

    Returns `empty` (the caller's empty/default payload) annotated with
    `degraded_reason`. This keeps the SPA's `data?.something.map(...)`
    safe from `o.map is not a function`.
    """
    code: str
    if isinstance(exc, ResourceNotFoundError):
        code = "not_found"
    elif isinstance(exc, HttpResponseError):
        status = getattr(exc, "status_code", None)
        if status == 403:
            code = "forbidden"
        elif status == 404:
            code = "not_found"
        else:
            code = f"http_{status or 'error'}"
    elif isinstance(exc, AzureError):
        code = "azure_error"
    else:
        code = type(exc).__name__
    LOGGER.warning("%s gracefully degraded: %s (%s)", op, code, sanitise(str(exc))[:200])
    out = dict(empty) if isinstance(empty, dict) else {"items": empty}
    out["degraded"] = True
    out["degraded_reason"] = code
    return out


# ---------------------------------------------------------------------------
# AKS
# ---------------------------------------------------------------------------
@router.get("/aks")
def list_aks(
    subscription_id: str = Query(default=""),
    resource_group: str = Query(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    sub = subscription_id or _sub_default()
    if not sub:
        raise HTTPException(400, "subscription_id required")
    cred = get_credential()
    try:
        clusters = monitoring_svc.list_aks_clusters(cred, sub, resource_group)
        return {"clusters": clusters}
    except Exception as exc:
        return _graceful("aks_list", exc, empty={"clusters": []})


@router.get("/aks/nodes")
def aks_nodes(
    subscription_id: str = Query(default=""),
    resource_group: str = Query(...),
    cluster_name: str = Query(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    sub = subscription_id or _sub_default()
    cred = get_credential()
    try:
        return {"nodes": monitoring_svc.k8s_get_nodes(cred, sub, resource_group, cluster_name)}
    except Exception as exc:
        return _graceful("aks_nodes", exc, empty={"nodes": []})


@router.get("/aks/pods")
def aks_pods(
    subscription_id: str = Query(default=""),
    resource_group: str = Query(...),
    cluster_name: str = Query(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    sub = subscription_id or _sub_default()
    cred = get_credential()
    try:
        return {"pods": monitoring_svc.k8s_get_pods(cred, sub, resource_group, cluster_name)}
    except Exception as exc:
        return _graceful("aks_pods", exc, empty={"pods": []})


@router.get("/aks/top-nodes")
def aks_top_nodes(
    subscription_id: str = Query(default=""),
    resource_group: str = Query(...),
    cluster_name: str = Query(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    sub = subscription_id or _sub_default()
    cred = get_credential()
    try:
        return {"nodes": monitoring_svc.k8s_top_nodes(cred, sub, resource_group, cluster_name)}
    except Exception as exc:
        return _graceful("aks_top_nodes", exc, empty={"nodes": []})


@router.get("/aks/pod-logs")
def aks_pod_logs(
    subscription_id: str = Query(default=""),
    resource_group: str = Query(...),
    cluster_name: str = Query(...),
    namespace: str = Query(...),
    pod_name: str = Query(...),
    tail: int = Query(default=200, ge=1, le=10000),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    sub = subscription_id or _sub_default()
    cred = get_credential()
    try:
        logs = monitoring_svc.k8s_pod_logs(
            cred, sub, resource_group, cluster_name, namespace, pod_name, tail
        )
        return {"logs": logs}
    except Exception as exc:
        return _graceful("aks_pod_logs", exc, empty={"logs": ""})


@router.get("/aks/service-ip")
def aks_service_ip(
    subscription_id: str = Query(default=""),
    resource_group: str = Query(...),
    cluster_name: str = Query(...),
    service_name: str = Query(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    sub = subscription_id or _sub_default()
    cred = get_credential()
    try:
        return monitoring_svc.k8s_get_service_ip(
            cred, sub, resource_group, cluster_name, service_name
        )
    except Exception as exc:
        return _graceful("aks_service_ip", exc, empty={"ip": None})


@router.get("/aks/warmup-status")
def aks_warmup_status(
    subscription_id: str = Query(default=""),
    resource_group: str = Query(...),
    cluster_name: str = Query(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    sub = subscription_id or _sub_default()
    cred = get_credential()
    try:
        return monitoring_svc.k8s_warmup_status(cred, sub, resource_group, cluster_name)
    except Exception as exc:
        return _graceful("aks_warmup_status", exc, empty={"databases": []})


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
@router.get("/storage")
def storage_summary(
    subscription_id: str = Query(default=""),
    resource_group: str = Query(...),
    account_name: str = Query(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    sub = subscription_id or _sub_default()
    cred = get_credential()
    try:
        return monitoring_svc.get_storage_summary(cred, sub, resource_group, account_name)
    except Exception as exc:
        return _graceful("storage_summary", exc, empty={"name": account_name, "containers": []})


# ---------------------------------------------------------------------------
# ACR
# ---------------------------------------------------------------------------
@router.get("/acr")
def list_acr(
    subscription_id: str = Query(default=""),
    resource_group: str = Query(...),
    registry_name: str = Query(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    sub = subscription_id or _sub_default()
    cred = get_credential()
    try:
        return {"repositories": monitoring_svc.list_acr_repositories(cred, sub, resource_group, registry_name)}
    except Exception as exc:
        return _graceful("list_acr", exc, empty={"repositories": []})


# ---------------------------------------------------------------------------
# Remote Terminal — there is no Remote Terminal VM in the new architecture.
# Return a stable shape so the legacy SPA card renders an "n/a" state.
# ---------------------------------------------------------------------------
@router.get("/terminal")
def terminal_status(
    subscription_id: str = Query(default=""),
    resource_group: str = Query(default=""),
    vm_name: str = Query(default=""),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    return {
        "vm_name": "",
        "power_state": "n/a",
        "provisioning_state": "n/a",
        "fqdn": "",
        "public_ip": "",
        "size": "",
        "degraded": True,
        "degraded_reason": "no_terminal_vm_in_container_apps_topology",
    }


# ---------------------------------------------------------------------------
# Cluster card (phase-0 stub, kept for legacy SPA paths)
# ---------------------------------------------------------------------------
@router.get("/cluster")
def cluster_stub(caller: CallerIdentity = Depends(require_caller)) -> dict[str, Any]:
    return {
        "status": "stub",
        "caller_oid": caller.object_id,
        "note": "use /api/monitor/aks?resource_group=... for real data",
    }


# ---------------------------------------------------------------------------
# Jobs (read jobstate from Storage table)
# ---------------------------------------------------------------------------
@router.get("/jobs")
def list_jobs(
    limit: int = Query(default=50, ge=1, le=200),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    try:
        from api_app.services.state_repo import JobStateRepository

        repo = JobStateRepository()
        rows = repo.list_for_owner(caller.object_id, limit=limit)
        return {
            "jobs": [
                {
                    "job_id": j.job_id,
                    "type": j.type,
                    "status": j.status,
                    "phase": j.phase,
                    "task_id": j.task_id,
                    "created_at": j.created_at,
                    "updated_at": j.updated_at,
                    "error_code": j.error_code,
                }
                for j in rows
            ]
        }
    except Exception as exc:
        return _graceful("list_jobs", exc, empty={"jobs": []})


@router.get("/jobs/{job_id}")
def get_job(
    job_id: str,
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    try:
        from api_app.services.state_repo import JobStateRepository

        repo = JobStateRepository()
        state = repo.get(job_id)
        if state is None:
            raise HTTPException(404, "job not found")
        if state.owner_oid and state.owner_oid != caller.object_id:
            raise HTTPException(403, "not owner")
        history = repo.get_history(job_id, limit=200)
        return {
            "state": {
                "job_id": state.job_id,
                "type": state.type,
                "status": state.status,
                "phase": state.phase,
                "task_id": state.task_id,
                "owner_oid": state.owner_oid,
                "tenant_id": state.tenant_id,
                "error_code": state.error_code,
                "created_at": state.created_at,
                "updated_at": state.updated_at,
                "payload": state.payload,
            },
            "history": history,
        }
    except HTTPException:
        raise
    except Exception as exc:
        return _graceful("get_job", exc, empty={"state": None, "history": []})
