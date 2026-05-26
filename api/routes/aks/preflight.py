"""AKS pre-flight + region-availability routes.

Two thin HTTP wrappers over `api.services.aks_availability`:

* ``GET  /api/aks/available-skus`` — used by the modal SKU dropdown so
  it can grey out SKUs the user's subscription is blocked from in the
  chosen region (instead of letting the user pick one and hit a 70 s
  ``BadRequest: VM size … is not allowed in your subscription`` round
  trip).
* ``POST /api/aks/preflight``      — runs SKU + quota + RG checks in
  one shot. The dashboard calls this *before* hitting
  ``/api/aks/provision`` so the user sees a structured pass/fail list
  with actionable messages instead of a generic "Provisioning… (70 s)
  → BadRequest".

Responsibility: HTTP shaping only. All Azure SDK calls live in
    `api.services.aks_availability`.
Edit boundaries: Keep these routers thin; new checks belong in the
    service.
Key entry points: `aks_available_skus`, `aks_preflight`.
Risky contracts: Both routes are auth-gated like every other
    `/api/aks/*` route. Preflight must never 500 on Azure SDK
    failures — degraded payload only.
Validation: `uv run pytest -q api/tests/test_aks_preflight_route.py
    api/tests/test_aks_available_skus_route.py`.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Body, Depends, Query

from api.auth import CallerIdentity, require_caller
from api.services import get_credential
from api.services.aks_availability import (
    azure_portal_aks_url,
    list_region_sku_availability,
    run_provision_preflight,
)
from api.services.aks_skus import (
    DEFAULT_SKU,
    DEFAULT_SYSTEM_NODE_COUNT,
    DEFAULT_SYSTEM_SKU,
)

LOGGER = logging.getLogger(__name__)

router = APIRouter()


@router.get("/available-skus")
def aks_available_skus(
    subscription_id: str = Query(...),
    region: str = Query(...),
    _caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Return per-SKU availability for the (subscription, region) pair."""
    credential = get_credential()
    availability = list_region_sku_availability(credential, subscription_id, region)
    available = sorted(name for name, sku in availability.items() if sku.available)
    unavailable = sorted(
        (asdict(sku) for sku in availability.values() if not sku.available),
        key=lambda r: r["name"],
    )
    return {
        "region": region,
        "available": available,
        "unavailable": unavailable,
        "degraded": not availability,
    }


@router.post("/preflight")
def aks_preflight(
    body: dict[str, Any] = Body(...),
    _caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Run SKU + quota + RG checks before enqueuing the provision task.

    The response shape mirrors the modal's progress list: `checks[]` is
    rendered top-to-bottom, each row carries a status (`ok` / `warn` /
    `fail`) and a human-readable message. `ok` is `false` whenever any
    row is `fail` — the FE blocks submit on that.
    """
    subscription_id = str(body.get("subscription_id") or "")
    region = str(body.get("region") or "")
    resource_group = str(body.get("resource_group") or "")
    cluster_name = str(body.get("cluster_name") or "")
    node_sku = str(body.get("node_sku") or DEFAULT_SKU)
    node_count = int(body.get("node_count") or 1)
    system_vm_size = str(body.get("system_vm_size") or DEFAULT_SYSTEM_SKU)
    system_node_count = int(body.get("system_node_count") or DEFAULT_SYSTEM_NODE_COUNT)

    credential = get_credential()
    ok, checks = run_provision_preflight(
        credential,
        subscription_id=subscription_id,
        resource_group=resource_group,
        region=region,
        node_sku=node_sku,
        node_count=node_count,
        system_vm_size=system_vm_size,
        system_node_count=system_node_count,
        acr_resource_group=str(body.get("acr_resource_group") or ""),
        acr_name=str(body.get("acr_name") or ""),
        storage_resource_group=str(body.get("storage_resource_group") or ""),
        storage_account=str(body.get("storage_account") or ""),
    )
    payload: dict[str, Any] = {
        "ok": ok,
        "checks": [
            {
                "name": c.name,
                "status": c.status,
                "message": c.message,
                "details": c.details,
            }
            for c in checks
        ],
        "portal_url": azure_portal_aks_url(subscription_id, resource_group, cluster_name)
        if subscription_id and resource_group and cluster_name
        else None,
    }
    return payload
