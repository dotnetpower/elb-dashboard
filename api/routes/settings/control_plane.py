"""Settings → Control plane domain HTTP routes.

Responsibility: HTTP shaping for the deployment-wide control-plane public URL —
    the custom domain an operator binds to the dashboard Container App and that
    the ElasticBLAST OpenAPI sibling webhooks back to (``CONTROL_PLANE_URL``).
    Read the configured + effective URL, persist a validated value, and clear
    it. All validation / persistence / resolution lives in
    ``api.services.control_plane_url``.
Edit boundaries: HTTP only — no durable-store or env logic inline. Every route
    enforces ``require_caller``.
Key entry points: ``get_status``, ``put_config``, ``clear_config``.
Risky contracts: PUT validates the URL server-side (``https://`` required except
    localhost, no path/query/fragment) and returns 503 when the durable store is
    unavailable so the SPA does not show a phantom save. The response never
    carries secrets — only the URL the operator typed and the resolved
    effective URL/source.
Validation: ``uv run pytest -q api/tests/test_settings_control_plane.py``.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Body, Depends, HTTPException

from api.auth import CallerIdentity, require_caller
from api.services.control_plane_url import (
    clear_control_plane_url,
    container_app_default_url,
    get_control_plane_url,
    normalise_control_plane_url,
    resolve_control_plane_url,
    save_control_plane_url,
)
from api.services.sanitise import redact_oid

LOGGER = logging.getLogger(__name__)

router = APIRouter()


def _status_dict() -> dict[str, Any]:
    """Build the shared status payload (configured + resolved effective URL)."""
    configured = get_control_plane_url()
    effective, source = resolve_control_plane_url()
    return {
        "configured_url": configured,
        "effective_url": effective,
        "source": source,
        "container_app_url": container_app_default_url(),
    }


@router.get("")
def get_status(_caller: CallerIdentity = Depends(require_caller)) -> dict[str, Any]:
    """Return the configured custom domain + the resolved effective URL.

    ``source`` tells the SPA where the effective URL came from: ``env`` (a
    ``DASHBOARD_PUBLIC_URL`` hard pin overrides the Settings value), ``settings``
    (this section's value is in effect), ``container_app`` (no custom domain —
    the auto-generated FQDN is used), or ``none`` (the sibling webhook is
    disabled). Never 404s — an unset value reads back as empty strings.
    """
    return _status_dict()


@router.put("")
def put_config(
    body: dict[str, Any] = Body(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Validate and persist the control-plane custom domain URL."""
    raw = str(body.get("url") or "").strip()
    if not raw:
        raise HTTPException(
            400, detail={"code": "url_required", "message": "url is required"}
        )
    try:
        normalised = normalise_control_plane_url(raw)
    except ValueError as exc:
        raise HTTPException(
            400, detail={"code": "invalid_url", "message": str(exc)}
        ) from exc
    if not normalised:
        raise HTTPException(
            400, detail={"code": "url_required", "message": "url is required"}
        )
    if not save_control_plane_url(normalised):
        raise HTTPException(
            503,
            detail={
                "code": "persist_failed",
                "message": (
                    "Durable store unavailable; the URL was not saved. Confirm "
                    "AZURE_TABLE_ENDPOINT is configured on the api sidecar."
                ),
            },
        )
    LOGGER.info(
        "control plane url set by oid=%s host=%s",
        redact_oid(caller.object_id),
        urlparse(normalised).hostname,
    )
    return {"status": "saved", **_status_dict()}


@router.delete("")
def clear_config(
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Clear the configured custom domain — resolution falls back to the FQDN."""
    clear_control_plane_url()
    LOGGER.info("control plane url cleared by oid=%s", redact_oid(caller.object_id))
    return {"status": "cleared", **_status_dict()}
