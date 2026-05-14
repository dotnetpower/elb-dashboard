"""Monitor endpoints — AKS / Storage / ACR / Terminal / Jobs.

Uses `api.services.monitoring`. The api sidecar talks to Azure with
the Container App's shared user-assigned managed identity for all SDK calls.

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
from fastapi import APIRouter, Body, Depends, HTTPException, Query

from api.auth import CallerIdentity, require_caller
from api.services import get_credential
from api.services import monitoring as monitoring_svc
from api.services.sanitise import sanitise

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
# AKS run-command — proxy kubectl commands via Kubernetes API
# ---------------------------------------------------------------------------
@router.post("/aks/run-command")
def aks_run_command(
    body: dict[str, Any] = Body(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Run a kubectl command on the AKS cluster via the Kubernetes API.

    Uses the k8s direct API helpers from monitoring service, NOT Azure Run Command
    (which is ~30s slow and ARM-rate-limited per copilot-instructions.md §11).
    """
    sub = body.get("subscription_id", "") or _sub_default()
    rg = body.get("resource_group", "")
    cluster_name = body.get("cluster_name", "")
    command = body.get("command", "")

    if not command or not rg or not cluster_name:
        return {"exit_code": 1, "output": "Missing required fields: resource_group, cluster_name, command"}

    cred = get_credential()
    try:
        # Use k8s_get_pods as a proxy for basic kubectl commands
        if command.strip().startswith("get pods") or command.strip().startswith("kubectl get pods"):
            namespace = body.get("namespace")
            pods = monitoring_svc.k8s_get_pods(cred, sub, rg, cluster_name, namespace=namespace)
            import json
            return {"exit_code": 0, "output": json.dumps(pods, indent=2, default=str)}
        elif command.strip().startswith("get nodes") or command.strip().startswith("kubectl get nodes"):
            nodes = monitoring_svc.k8s_get_nodes(cred, sub, rg, cluster_name)
            import json
            return {"exit_code": 0, "output": json.dumps(nodes, indent=2, default=str)}
        elif command.strip().startswith("top nodes") or command.strip().startswith("kubectl top nodes"):
            metrics = monitoring_svc.k8s_top_nodes(cred, sub, rg, cluster_name)
            import json
            return {"exit_code": 0, "output": json.dumps(metrics, indent=2, default=str)}
        else:
            return {"exit_code": 1, "output": f"Command not supported via API proxy. Supported: get pods, get nodes, top nodes. Use the Browser Terminal for arbitrary kubectl commands."}
    except Exception as exc:
        return {"exit_code": 1, "output": f"Error: {type(exc).__name__}: {str(exc)[:500]}"}


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
        return monitoring_svc.list_acr_repositories(cred, sub, resource_group, registry_name)
    except Exception as exc:
        return _graceful("list_acr", exc, empty={"name": registry_name, "login_server": "", "sku": None, "expected_image_tags": {}, "actual_tags": {}, "building_images": [], "build_details": []})


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
        from api.services.state_repo import JobStateRepository

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
        from api.services.state_repo import JobStateRepository

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


# ---------------------------------------------------------------------------
# Control-plane sidecars — snapshot + ticket + SSE
#
# Browsers cannot attach Authorization headers to `EventSource`, so we mirror
# the ticket pattern from /api/terminal/ws: the SPA POSTs to /sidecars/ticket
# with its bearer, gets a single-use opaque token back, then connects to
# /sidecars/events?ticket=... — the GET handler validates the ticket
# without re-reading the bearer.
# ---------------------------------------------------------------------------
import asyncio  # noqa: E402 — keep import local to the section that uses it
import json as _json  # noqa: E402
import secrets  # noqa: E402
import time as _time  # noqa: E402
from dataclasses import dataclass  # noqa: E402

from fastapi.responses import StreamingResponse  # noqa: E402

from api.services.sidecar_metrics import collect_snapshot  # noqa: E402

_SIDECAR_TICKET_TTL_SEC = 30
_SSE_PUSH_INTERVAL_SEC = 5
_SSE_HEARTBEAT_INTERVAL_SEC = 25  # < Container Apps' 240s idle ws timeout.


@dataclass(frozen=True)
class _SidecarTicket:
    owner_oid: str
    expires_at: float


_sidecar_tickets: dict[str, _SidecarTicket] = {}
_sidecar_tickets_lock = asyncio.Lock()


@router.get("/sidecars")
async def sidecars_snapshot(
    _caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """One-shot snapshot of all sidecars' health + CPU/MEM. Used by the SPA
    on initial card mount and as the polling fallback when SSE fails.
    """
    try:
        return collect_snapshot()
    except Exception as exc:  # noqa: BLE001
        return _graceful(
            "sidecars_snapshot",
            exc,
            empty={
                "ts": None,
                "revision": os.environ.get("CONTAINER_APP_REVISION", "local"),
                "sidecars": {},
            },
        )


@router.post("/sidecars/ticket")
async def sidecars_ticket(
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, object]:
    """Validate the bearer and issue a short-lived single-use SSE ticket."""
    token = secrets.token_urlsafe(24)
    async with _sidecar_tickets_lock:
        now = _time.time()
        # Reap expired tickets opportunistically.
        for k in [k for k, v in _sidecar_tickets.items() if v.expires_at <= now]:
            _sidecar_tickets.pop(k, None)
        _sidecar_tickets[token] = _SidecarTicket(
            owner_oid=caller.object_id,
            expires_at=now + _SIDECAR_TICKET_TTL_SEC,
        )
    return {"ticket": token, "ttl_seconds": _SIDECAR_TICKET_TTL_SEC}


async def _consume_sidecar_ticket(token: str | None) -> _SidecarTicket:
    if not token:
        raise HTTPException(401, "ticket required")
    async with _sidecar_tickets_lock:
        entry = _sidecar_tickets.pop(token, None)
    if entry is None:
        raise HTTPException(401, "invalid or expired ticket")
    if entry.expires_at <= _time.time():
        raise HTTPException(401, "ticket expired")
    return entry


@router.get("/sidecars/events")
async def sidecars_events(ticket: str | None = Query(default=None)):
    """Server-Sent Events stream of sidecar metric snapshots.

    Protocol:
      * Every 5s: ``event: snapshot`` followed by the same JSON shape as
        the GET /sidecars endpoint.
      * Every 25s of idle: ``: heartbeat`` comment line so Container Apps'
        proxy keeps the connection alive (idle ws/SSE timeout is 240s).
      * Client should reconnect on any close (TanStack Query / EventSource
        does this automatically with ``last-event-id``).
    """
    await _consume_sidecar_ticket(ticket)

    async def event_stream():
        # Initial snapshot — emits within ~50 ms of connect for fast first paint.
        try:
            initial = collect_snapshot()
            yield f"event: snapshot\ndata: {_json.dumps(initial)}\n\n"
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("sidecars_events: initial snapshot failed: %s", exc)
            yield 'event: error\ndata: {"code":"snapshot_failed"}\n\n'

        last_push = asyncio.get_event_loop().time()
        while True:
            now_loop = asyncio.get_event_loop().time()
            # Heartbeat keep-alive when no real event in a while.
            if now_loop - last_push >= _SSE_HEARTBEAT_INTERVAL_SEC:
                yield ": heartbeat\n\n"
                last_push = now_loop
                continue
            await asyncio.sleep(_SSE_PUSH_INTERVAL_SEC)
            try:
                snap = collect_snapshot()
                yield f"event: snapshot\ndata: {_json.dumps(snap)}\n\n"
                last_push = asyncio.get_event_loop().time()
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("sidecars_events: tick failed: %s", exc)
                yield 'event: error\ndata: {"code":"tick_failed"}\n\n'

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",  # disable proxy buffering
            "Connection": "keep-alive",
        },
    )
