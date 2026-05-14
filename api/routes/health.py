"""Liveness + readiness endpoints. No auth required — used by Container Apps health probes."""

from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import APIRouter

from api import __version__

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
def readiness() -> dict[str, Any]:
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
        components["terminal_sidecar"] = {"status": "ok" if th.get("status") == "ok" else "degraded"}
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
    except Exception as exc:  # noqa: BLE001
        out["errors"].append(f"producer_conf: {type(exc).__name__}: {exc}")

    # 1. Redis queue lengths via raw redis client (no kombu wrapper)
    try:
        import redis as _redis
        from api.celery_app import CELERY_BROKER_URL
        r = _redis.Redis.from_url(CELERY_BROKER_URL, socket_timeout=2)
        for q in ("default", "azure", "blast", "storage", "celery"):
            try:
                out["queues"][q] = r.llen(q)
            except Exception as exc:  # noqa: BLE001
                out["queues"][q] = f"err:{type(exc).__name__}"
        out["broker_url"] = CELERY_BROKER_URL
        out["redis_keys_db0"] = sorted(
            k.decode() for k in r.keys("*") if not k.startswith(b"_kombu")
        )
    except Exception as exc:  # noqa: BLE001
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
    except Exception as exc:  # noqa: BLE001
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
