"""ARM discovery + Resource Group tag routes (`/api/arm/*`).

Responsibility: ARM discovery + Resource Group tag routes (`/api/arm/*`)
Edit boundaries: Keep HTTP validation and response shaping here; move cloud/data-plane work into
services or tasks.
Key entry points: `list_subscriptions`, `list_resource_groups`, `get_rg_tags`, `set_rg_tags`,
`list_storage_accounts`, `list_acrs`
Risky contracts: Every non-health `/api/*` route must enforce `require_caller` or an equivalent
auth gate.
Validation: `uv run pytest -q api/tests/test_route_contracts.py`.
"""

from __future__ import annotations

import logging
from typing import Any, cast

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query

from api.auth import CallerIdentity, require_caller
from api.routes.monitor import _graceful  # reuse degraded-response helper
from api.services import get_credential
from api.services.azure_clients import (
    acr_client,
    compute_client,
    resource_client,
    storage_client,
)
from api.services.sanitise import sanitise

LOGGER = logging.getLogger(__name__)

router = APIRouter(prefix="/api/arm", tags=["arm"])

ELB_TAG_PREFIX = "elb-"


@router.get("/subscriptions")
def list_subscriptions(
    caller: CallerIdentity = Depends(require_caller),
) -> list[dict[str, Any]]:
    """List subscriptions visible to the api sidecar's managed identity."""
    from azure.mgmt.resource import SubscriptionClient

    cred = get_credential()
    try:
        client = SubscriptionClient(cred)
        subs: list[dict[str, Any]] = []
        for s in client.subscriptions.list():
            state = s.state
            subs.append(
                {
                    "subscriptionId": s.subscription_id,
                    "displayName": s.display_name,
                    "state": state.value if hasattr(state, "value") else str(state or "Unknown"),
                    "tenantId": s.tenant_id,
                }
            )
        subs.sort(key=lambda x: x["displayName"])
        return subs
    except Exception as exc:
        LOGGER.warning(
            "list_subscriptions failed: %s: %s",
            type(exc).__name__,
            sanitise(str(exc)),
            exc_info=True,
        )
        # Subscriptions list is critical for the SPA's first render. Returning
        # an empty array (rather than 500) lets the SPA show "no subscriptions
        # available" with a Reload action instead of crashing.
        return []


@router.get("/subscriptions/{subscription_id}/resource-groups")
def list_resource_groups(
    subscription_id: str = Path(...),
    caller: CallerIdentity = Depends(require_caller),
) -> list[dict[str, Any]]:
    cred = get_credential()
    try:
        rc = resource_client(cred, subscription_id)
        groups = [
            {"name": g.name, "location": g.location, "tags": g.tags or {}}
            for g in rc.resource_groups.list()
        ]
        groups.sort(key=lambda x: x["name"])
        return groups
    except Exception as exc:
        LOGGER.warning(
            "list_resource_groups failed: %s: %s",
            type(exc).__name__,
            sanitise(str(exc)),
            exc_info=True,
        )
        return []


@router.get("/resource-group/tags")
def get_rg_tags(
    subscription_id: str = Query(...),
    resource_group: str = Query(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    cred = get_credential()
    try:
        rc = resource_client(cred, subscription_id)
        rg = rc.resource_groups.get(resource_group)
        tags = {k: v for k, v in (rg.tags or {}).items() if k.startswith(ELB_TAG_PREFIX)}
        return {"resource_group": rg.name, "tags": tags}
    except Exception as exc:
        return cast(
            dict[str, Any],
            _graceful("get_rg_tags", exc, empty={"resource_group": resource_group, "tags": {}}),
        )


@router.post("/resource-group/tags")
def set_rg_tags(
    body: dict[str, Any] = Body(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    sub = body.get("subscription_id", "")
    rg_name = body.get("resource_group", "")
    new_tags: dict[str, str] = body.get("tags", {})
    if not sub or not rg_name or not new_tags:
        raise HTTPException(400, "subscription_id, resource_group, tags required")
    for k in new_tags:
        if not k.startswith(ELB_TAG_PREFIX):
            raise HTTPException(400, f"tag key must start with '{ELB_TAG_PREFIX}': {k}")
    cred = get_credential()
    try:
        rc = resource_client(cred, sub)
        rg = rc.resource_groups.get(rg_name)
        merged = {**(rg.tags or {}), **new_tags}
        rc.resource_groups.create_or_update(rg_name, {"location": rg.location, "tags": merged})
        return {
            "resource_group": rg_name,
            "tags": {k: v for k, v in merged.items() if k.startswith(ELB_TAG_PREFIX)},
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, sanitise(str(exc))) from exc


@router.get("/subscriptions/{subscription_id}/resource-groups/{rg}/storage-accounts")
def list_storage_accounts(
    subscription_id: str = Path(...),
    rg: str = Path(...),
    caller: CallerIdentity = Depends(require_caller),
) -> list[dict[str, Any]]:
    cred = get_credential()
    try:
        client = storage_client(cred, subscription_id)
        accounts = [
            {
                "name": a.name,
                "location": a.location,
                "isHnsEnabled": getattr(a, "is_hns_enabled", None),
            }
            for a in client.storage_accounts.list_by_resource_group(rg)
        ]
        accounts.sort(key=lambda x: x["name"])
        return accounts
    except Exception as exc:
        LOGGER.warning(
            "list_storage_accounts failed: %s: %s",
            type(exc).__name__,
            sanitise(str(exc)),
            exc_info=True,
        )
        return []


@router.get("/subscriptions/{subscription_id}/resource-groups/{rg}/acrs")
def list_acrs(
    subscription_id: str = Path(...),
    rg: str = Path(...),
    caller: CallerIdentity = Depends(require_caller),
) -> list[dict[str, Any]]:
    cred = get_credential()
    try:
        client = acr_client(cred, subscription_id)
        registries = [
            {"name": r.name, "location": r.location, "loginServer": r.login_server}
            for r in client.registries.list_by_resource_group(rg)
        ]
        registries.sort(key=lambda x: x["name"])
        return registries
    except Exception as exc:
        LOGGER.warning(
            "list_acrs failed: %s: %s",
            type(exc).__name__,
            sanitise(str(exc)),
            exc_info=True,
        )
        return []


@router.get("/subscriptions/{subscription_id}/resource-groups/{rg}/vms")
def list_vms(
    subscription_id: str = Path(...),
    rg: str = Path(...),
    caller: CallerIdentity = Depends(require_caller),
) -> list[dict[str, Any]]:
    cred = get_credential()
    try:
        client = compute_client(cred, subscription_id)
        vms = [{"name": v.name, "location": v.location} for v in client.virtual_machines.list(rg)]
        vms.sort(key=lambda x: x["name"])
        return vms
    except Exception as exc:
        LOGGER.warning(
            "list_vms failed: %s: %s",
            type(exc).__name__,
            sanitise(str(exc)),
            exc_info=True,
        )
        return []
