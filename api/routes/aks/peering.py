"""AKS VNet peering recovery route.

Responsibility: Synchronous "peer this cluster's VNet with the dashboard platform VNet"
    endpoint for existing AKS clusters created before the auto-peering step in
    `provision_aks` shipped (2026-05-27). Wraps the same idempotent helper the
    provision task calls, so the result shape matches.
Edit boundaries: HTTP input validation + response shaping only. All Azure SDK work
    happens in `api.tasks.azure.peering.ensure_vnet_peering_with_cluster`.
Key entry points: `aks_peer_with_platform`.
Risky contracts: Every non-health `/api/*` route must enforce `require_caller`.
    The endpoint is synchronous (peering CRUD typically returns in 5-15 s) — do not
    enqueue to Celery without changing the SPA contract.
Validation: `uv run pytest -q api/tests/test_aks_peering_route.py`.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException

from api.auth import CallerIdentity, require_caller

LOGGER = logging.getLogger(__name__)

router = APIRouter()


@router.post("/peer-with-platform")
def aks_peer_with_platform(
    body: dict[str, Any] = Body(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Peer the platform VNet with the AKS cluster's auto-VNet (recovery).

    Idempotent: re-runs against an already-peered pair are a no-op.
    Use this from the SPA when ``/api/aks/openapi/{proxy,spec}`` returns
    a 502/timeout but the ``elb-openapi`` pods + Service are healthy.
    Existing clusters created before the auto-peering step in
    ``provision_aks`` (2026-05-27) hit this exact gap; new clusters fix
    it automatically during create.
    """

    cluster_name = (body.get("cluster_name") or "").strip()
    resource_group = (body.get("resource_group") or "").strip()
    if not (cluster_name and resource_group):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "missing_parameters",
                "message": "resource_group and cluster_name are required.",
            },
        )
    subscription_id = (
        body.get("subscription_id") or os.getenv("AZURE_SUBSCRIPTION_ID", "") or ""
    ).strip()
    if not subscription_id:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "missing_parameters",
                "message": (
                    "subscription_id is required (env AZURE_SUBSCRIPTION_ID "
                    "is not set in this sidecar)."
                ),
            },
        )

    LOGGER.info(
        "aks/peer-with-platform requested cluster=%s rg=%s caller_oid=%s",
        cluster_name,
        resource_group,
        caller.object_id,
    )

    from api.services import get_credential
    from api.tasks.azure.peering import ensure_vnet_peering_with_cluster

    try:
        summary = ensure_vnet_peering_with_cluster(
            get_credential(),
            subscription_id=subscription_id,
            cluster_resource_group=resource_group,
            cluster_name=cluster_name,
        )
    except Exception as exc:
        # Hard failure inside the helper (e.g. credential lookup blew up).
        # `ensure_vnet_peering_with_cluster` already absorbs per-peering
        # failures into `error`/`recovery_command`; an exception here
        # means something else broke. Surface 502 with a recovery hint
        # rather than a raw 500.
        LOGGER.exception("aks/peer-with-platform: helper raised")
        raise HTTPException(
            status_code=502,
            detail={
                "code": "vnet_peering_unavailable",
                "message": (
                    "VNet peering could not be evaluated: "
                    f"{type(exc).__name__}: {str(exc)[:200]}"
                ),
            },
        ) from exc

    return summary
