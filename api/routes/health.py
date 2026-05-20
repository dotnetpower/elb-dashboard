"""Liveness + readiness endpoints. No auth required - used by Container Apps health probes.

Responsibility: Liveness + readiness endpoints. No auth required - used by Container Apps health
probes
Edit boundaries: Keep HTTP validation and response shaping here; move cloud/data-plane work into
services or tasks.
Key entry points: `health`, `readiness`, `celery_diag`, `celery_enqueue_noop`,
`celery_task_result`, `azure_discovery_probe`
Risky contracts: Every non-health `/api/*` route must enforce `require_caller` or an equivalent
auth gate.
Validation: `uv run pytest -q api/tests/test_route_contracts.py`.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends

from api import __version__
from api.auth import require_caller

if TYPE_CHECKING:
    from api.auth import CallerIdentity


# Re-export with a private alias so the dependency is unambiguous in the
# diagnostic route signature (the public name is still ``require_caller``).
_require_caller_lazy = require_caller

LOGGER = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict[str, str]:
    """Liveness probe — return 200 if the process can answer.

    Container Apps uses HTTP probes against this path. Keep the response
    cheap — no Azure SDK calls, no Storage reads, no token validation.
    """
    return {
        "status": "ok",
        "version": __version__,
        "revision": os.environ.get("CONTAINER_APP_REVISION", "local"),
    }


@router.get("/health/ready")
def readiness() -> Any:
    """Readiness/deep health probe — checks downstream dependencies.

    Returns 200 with component statuses when the service can accept work.
    Returns 503 when critical dependencies are down.  The response always
    includes ``retryable`` and ``retry_after_seconds`` hints so the caller
    (SPA or load balancer) knows whether to wait or fail fast.
    """
    components: dict[str, dict[str, Any]] = {}
    overall_ok = True

    # 1. Redis (Celery broker)
    try:
        from api.celery_app import celery_app

        conn = celery_app.connection()
        conn.ensure_connection(max_retries=1, timeout=2)
        conn.close()
        components["redis"] = {"status": "ok"}
    except Exception as exc:
        components["redis"] = {"status": "down", "error": str(exc)[:200]}
        overall_ok = False

    # 2. Azure credential (can we get a token?)
    try:
        from api.services import get_credential

        cred = get_credential()
        # Just check the credential object exists — don't make an ARM call
        components["azure_credential"] = {"status": "ok", "type": type(cred).__name__}
    except Exception as exc:
        components["azure_credential"] = {"status": "down", "error": str(exc)[:200]}
        overall_ok = False

    # 3. Terminal sidecar
    try:
        from api.services.terminal_exec import healthz

        th = healthz()
        components["terminal_sidecar"] = {
            "status": "ok" if th.get("status") == "ok" else "degraded"
        }
    except Exception:
        components["terminal_sidecar"] = {"status": "down"}
        # Non-critical — terminal being down doesn't block BLAST submit

    status_code = 200 if overall_ok else 503
    from fastapi.responses import JSONResponse

    return JSONResponse(
        content={
            "status": "ready" if overall_ok else "not_ready",
            "version": __version__,
            "components": components,
            "retryable": not overall_ok,
            "retry_after_seconds": 30 if not overall_ok else None,
        },
        status_code=status_code,
    )


@router.get("/health/celery")
def celery_diag() -> dict[str, Any]:
    """Diagnostic-only: return per-queue length + worker inspect snapshot.

    Unauthenticated by design (read-only, no secrets in payload). Used to
    diagnose 'task enqueued but worker silent' problems. Kept after the
    2026-05-15 routing-fix so future drift can be spotted in seconds:
    `redis_keys_db0` should contain only the worker's known queue names.
    """
    out: dict[str, Any] = {"queues": {}, "workers": None, "errors": []}

    # 0. Producer-side celery config (so we can spot a config mismatch
    #    between api and worker sidecars).
    try:
        from api.celery_app import celery_app

        out["producer_conf"] = {
            "default_queue": celery_app.conf.task_default_queue,
            "task_routes": dict(celery_app.conf.task_routes or {}),
            "broker_url": celery_app.conf.broker_url,
        }
    except Exception as exc:
        out["errors"].append(f"producer_conf: {type(exc).__name__}: {exc}")

    # 1. Redis queue lengths via raw redis client (no kombu wrapper)
    try:
        import redis as _redis

        from api.celery_app import CELERY_BROKER_URL

        r = _redis.Redis.from_url(CELERY_BROKER_URL, socket_timeout=2)
        for q in ("default", "azure", "blast", "storage", "celery"):
            try:
                out["queues"][q] = r.llen(q)
            except Exception as exc:
                out["queues"][q] = f"err:{type(exc).__name__}"
        out["broker_url"] = CELERY_BROKER_URL
        out["redis_keys_db0"] = sorted(
            k.decode() for k in r.keys("*") if not k.startswith(b"_kombu")  # type: ignore[union-attr]
        )
    except Exception as exc:
        out["errors"].append(f"redis: {type(exc).__name__}: {exc}")

    # 2. Celery worker inspect (active / reserved / scheduled / registered)
    try:
        from api.celery_app import celery_app

        insp = celery_app.control.inspect(timeout=2)
        out["workers"] = {
            "active": insp.active(),
            "reserved": insp.reserved(),
            "scheduled": insp.scheduled(),
            "registered": insp.registered(),
            "stats": insp.stats(),
            "ping": insp.ping(),
        }
    except Exception as exc:
        out["errors"].append(f"inspect: {type(exc).__name__}: {exc}")

    return out


@router.post("/health/celery/enqueue-noop")
def celery_enqueue_noop(message: str = "diag-ping") -> dict[str, Any]:
    """Diagnostic-only: enqueue a no-op task. Returns task_id for status polling."""
    from api.tasks.azure import diag_noop

    res = diag_noop.delay(message=message)
    return {"task_id": res.id, "queue": "azure", "message": message}


@router.get("/health/celery/result/{task_id}")
def celery_task_result(task_id: str) -> dict[str, Any]:
    """Diagnostic-only: get a celery task result without auth."""
    from celery.result import AsyncResult

    from api.celery_app import celery_app

    r = AsyncResult(task_id, app=celery_app)
    out: dict[str, Any] = {
        "task_id": task_id,
        "status": r.status,
        "ready": r.ready(),
    }
    if r.ready():
        if r.successful():
            out["result"] = r.result
        else:
            out["error"] = str(r.result)[:500]
    return out


@router.get("/health/azure-discovery")
def azure_discovery_probe(
    caller: CallerIdentity = Depends(_require_caller_lazy),
) -> dict[str, Any]:
    """Diagnostic-only: prove the api can list subscriptions / RGs end-to-end.

    Why this exists: the SPA's discovery wizard fails silently when the
    api sidecar's credential cannot reach ARM (no MI attached, MI lacks
    Reader at subscription scope, IMDS not responding, ...). The user-
    visible symptom is an empty list with no error. This endpoint forces
    each step of the chain and reports which one fails so the operator
    has a single curl-able answer.

    Auth-gated (MSAL bearer required — unlike the other /health/* probes)
    because the response references subscription ids and would otherwise
    leak tenant topology to anyone who can reach the ingress. All sub
    ids and display names that appear in the response go through
    `sanitise()` so even an authenticated caller never sees raw GUIDs.

    Makes two real ARM calls (subscriptions.list with a hard cap of 5,
    then resource_groups.list on the first sub) so do not poll this
    from a dashboard — use it as a one-shot post-deploy sanity check.
    """
    from azure.mgmt.resource import SubscriptionClient

    from api.services import get_credential
    from api.services.azure_clients import resource_client
    from api.services.sanitise import sanitise

    _ = caller  # accepted for auth-gate side effect; not echoed back

    out: dict[str, Any] = {
        "credential": {"status": "unknown"},
        "subscriptions_list": {"status": "unknown"},
        "resource_groups_list": {"status": "unknown"},
        "hint": None,
    }

    # 1. Credential acquisition (does NOT call IMDS yet — just constructs).
    try:
        cred = get_credential()
        out["credential"] = {"status": "ok", "type": type(cred).__name__}
    except Exception as exc:
        out["credential"] = {
            "status": "error",
            "error_type": type(exc).__name__,
            "error_message": sanitise(str(exc))[:300],
        }
        out["hint"] = (
            "DefaultAzureCredential could not be constructed. Check that the "
            "azure-identity package is installed in the api image."
        )
        return out

    # 2. Subscriptions list (forces a real ARM token via IMDS / MI).
    sub_id: str | None = None
    try:
        client = SubscriptionClient(cred)
        # Hard cap so a misconfigured tenant with thousands of subs does
        # not turn this probe into an outage.
        samples: list[dict[str, str]] = []
        first_sub_id: str | None = None
        for i, s in enumerate(client.subscriptions.list()):
            if i >= 5:
                break
            if first_sub_id is None:
                first_sub_id = s.subscription_id
            # Always sanitise: response is auth-gated but we still mask GUIDs
            # so the diagnostic blob can be safely pasted into a bug report
            # or a public log channel. Display names are dropped entirely
            # (they often encode tenant/org names or billing context).
            samples.append({"id": sanitise(s.subscription_id or "")})
        out["subscriptions_list"] = {
            "status": "ok",
            "count_capped_at_5": len(samples),
            "samples": samples,
        }
        sub_id = first_sub_id  # raw (used for the next ARM call only, never echoed)
    except Exception as exc:
        out["subscriptions_list"] = {
            "status": "error",
            "error_type": type(exc).__name__,
            "error_message": sanitise(str(exc))[:300],
        }
        out["hint"] = (
            "subscriptions.list() failed. Most likely the user-assigned MI is "
            "not attached to this Container App, or the AZURE_CLIENT_ID env "
            "does not match the MI's clientId. Verify with `az containerapp "
            "show --name ca-elb-control --query identity` and "
            "`az containerapp show ... --query 'properties.template.containers[0].env'`."
        )
        return out

    if not sub_id:
        out["resource_groups_list"] = {"status": "skipped", "reason": "no subscriptions visible"}
        out["hint"] = (
            "Credential works but the MI sees zero subscriptions. Confirm "
            "subscription-scope Reader was granted (infra/modules/"
            "subscriptionRoles.bicep, or `az role assignment create --role "
            "Reader --scope /subscriptions/<id> --assignee-object-id <miOid>`)."
        )
        return out

    # 3. Resource group list — proves Reader/Contributor at sub scope.
    try:
        rc = resource_client(cred, sub_id)
        rg_count = sum(1 for _ in rc.resource_groups.list())
        out["resource_groups_list"] = {
            "status": "ok",
            "subscription_id": sanitise(sub_id),
            "count": rg_count,
        }
        if rg_count == 0:
            out["hint"] = (
                "subscriptions.list() works but resource_groups.list() returned 0. "
                "Either the subscription truly is empty, or the MI has only a "
                "resource-scope role (Storage/ACR/KV) which is enough to surface "
                "the subscription but not to enumerate RGs. Grant Reader at "
                "subscription scope."
            )
    except Exception as exc:
        out["resource_groups_list"] = {
            "status": "error",
            "subscription_id": sanitise(sub_id),
            "error_type": type(exc).__name__,
            "error_message": sanitise(str(exc))[:300],
        }
        out["hint"] = (
            "resource_groups.list() failed. Check that the MI has Reader (or "
            "higher) at subscription scope — see docs/auth.md §Step 2."
        )

    return out
