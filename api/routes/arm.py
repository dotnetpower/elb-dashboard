"""ARM discovery + Resource Group tag routes (`/api/arm/*`).

Responsibility: ARM discovery + Resource Group tag routes (`/api/arm/*`)
Edit boundaries: Keep HTTP validation and response shaping here; move cloud/data-plane work into
services or tasks.
Key entry points: `list_subscriptions`, `list_resource_groups`, `list_locations`, `get_rg_tags`,
`set_rg_tags`, `list_storage_accounts`, `list_acrs`
Risky contracts: Every non-health `/api/*` route must enforce `require_caller` or an equivalent
auth gate.
Validation: `uv run pytest -q api/tests/test_route_contracts.py`.
"""

from __future__ import annotations

import logging
import time
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
_DISCOVERY_CACHE_TTL_SECONDS = 60.0
_DISCOVERY_CACHE: dict[tuple[str, str, str], tuple[float, list[dict[str, Any]]]] = {}


def _cached_discovery(kind: str, subscription_id: str, rg: str) -> list[dict[str, Any]] | None:
    cached = _DISCOVERY_CACHE.get((kind, subscription_id, rg))
    if cached is None:
        return None
    expires_at, value = cached
    if expires_at <= time.monotonic():
        _DISCOVERY_CACHE.pop((kind, subscription_id, rg), None)
        return None
    return [dict(item) for item in value]


def _store_discovery(
    kind: str, subscription_id: str, rg: str, value: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    _DISCOVERY_CACHE[(kind, subscription_id, rg)] = (
        time.monotonic() + _DISCOVERY_CACHE_TTL_SECONDS,
        [dict(item) for item in value],
    )
    return value

# Azure ARM tag limits (Microsoft Learn):
# - Tag name: 1..512 characters; cannot contain ``<>%&\?/``
# - Tag value: 0..256 characters
# - Tags per resource: max 50
# Validate at the api boundary so a malformed POST cannot turn into an
# Azure SDK exception that leaks request ids / server messages into the
# response body. The ELB_TAG_PREFIX check above limits *which* tag names
# the dashboard can write, but does not limit *length* or *content*.
_TAG_NAME_MAX_LEN = 512
_TAG_VALUE_MAX_LEN = 256
_TAG_MAX_PER_REQUEST = 50
_TAG_NAME_FORBIDDEN_CHARS = set("<>%&\\?/")


def _validate_tag_name(key: str) -> None:
    if not key:
        raise HTTPException(400, "tag name must not be empty")
    if len(key) > _TAG_NAME_MAX_LEN:
        raise HTTPException(
            400, f"tag name exceeds {_TAG_NAME_MAX_LEN} characters: {key[:40]}..."
        )
    bad = _TAG_NAME_FORBIDDEN_CHARS.intersection(key)
    if bad:
        raise HTTPException(
            400,
            f"tag name {key!r} contains characters Azure rejects: {sorted(bad)}",
        )
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in key):
        raise HTTPException(400, f"tag name {key!r} contains control characters")


def _validate_tag_value(key: str, value: str) -> None:
    if value is None:
        return
    if not isinstance(value, str):
        raise HTTPException(400, f"tag value for {key!r} must be a string")
    if len(value) > _TAG_VALUE_MAX_LEN:
        raise HTTPException(
            400,
            f"tag value for {key!r} exceeds {_TAG_VALUE_MAX_LEN} characters",
        )
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise HTTPException(400, f"tag value for {key!r} contains control characters")


@router.get("/subscriptions")
def list_subscriptions(
    caller: CallerIdentity = Depends(require_caller),
) -> list[dict[str, Any]]:
    """List subscriptions visible to the api sidecar's managed identity."""
    from api.services.azure_clients import subscription_client

    cred = get_credential()
    try:
        client = subscription_client(cred)
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


@router.get("/subscriptions/{subscription_id}/locations")
def list_locations(
    subscription_id: str = Path(...),
    caller: CallerIdentity = Depends(require_caller),
) -> list[dict[str, Any]]:
    """Locations the given subscription can deploy to.

    Lets the provision-AKS modal show a region dropdown that reflects the
    subscription's actual allow-list instead of a hard-coded SPA constant.
    Returns an empty array on failure so the SPA can fall back to its
    bundled `AZURE_REGIONS` list.
    """
    from api.services.azure_clients import subscription_client

    cred = get_credential()
    try:
        client = subscription_client(cred)
        locations: list[dict[str, Any]] = []
        for loc in client.subscriptions.list_locations(subscription_id):
            # Skip non-physical regions (`category != "Recommended"` includes
            # logical/extended zones the AKS control plane refuses to host).
            metadata = getattr(loc, "metadata", None)
            region_type = getattr(metadata, "region_type", None) if metadata else None
            if region_type and region_type != "Physical":
                continue
            locations.append(
                {
                    "name": loc.name,
                    "displayName": loc.display_name,
                    "regionalDisplayName": getattr(loc, "regional_display_name", None)
                    or loc.display_name,
                }
            )
        locations.sort(key=lambda x: x["displayName"])
        return locations
    except Exception as exc:
        LOGGER.warning(
            "list_locations failed: %s: %s",
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
    if not isinstance(new_tags, dict):
        raise HTTPException(400, "tags must be an object")
    if len(new_tags) > _TAG_MAX_PER_REQUEST:
        raise HTTPException(
            400,
            f"too many tags in one request ({len(new_tags)} > {_TAG_MAX_PER_REQUEST})",
        )
    for k, v in new_tags.items():
        if not k.startswith(ELB_TAG_PREFIX):
            raise HTTPException(400, f"tag key must start with '{ELB_TAG_PREFIX}': {k}")
        _validate_tag_name(k)
        _validate_tag_value(k, v)
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
    cached = _cached_discovery("storage", subscription_id, rg)
    if cached is not None:
        return cached
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
        return _store_discovery("storage", subscription_id, rg, accounts)
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
    cached = _cached_discovery("acr", subscription_id, rg)
    if cached is not None:
        return cached
    cred = get_credential()
    try:
        client = acr_client(cred, subscription_id)
        registries = [
            {"name": r.name, "location": r.location, "loginServer": r.login_server}
            for r in client.registries.list_by_resource_group(rg)
        ]
        registries.sort(key=lambda x: x["name"])
        return _store_discovery("acr", subscription_id, rg, registries)
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
