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
