"""Storage local-debug public access routes."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException

from api.auth import CallerIdentity, require_caller
from api.routes.storage.common import _RE_RG, _RE_STORAGE_ACCOUNT, _RE_SUB, _check
from api.services import get_credential

LOGGER = logging.getLogger(__name__)

router = APIRouter()


@router.get("/local-debug")
def storage_local_debug_status(
    subscription_id: str = "",
    resource_group: str = "",
    account_name: str = "",
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Return whether the dashboard should expose the local-debug toggle.

    Always returns 200. ``is_local`` is the only field the SPA needs to gate
    the button visibility. ``public_access``, ``default_action``, and
    ``ip_rules`` are best-effort context for the confirmation modal.
    """
    from api.services.storage_public_access import (
        is_running_locally,
        read_local_storage_state,
    )

    if not is_running_locally():
        # Deployed: the toggle button must never appear in the UI.
        return {"is_local": False}

    if not (subscription_id and resource_group and account_name):
        # Local but no account scope yet — still tell the SPA we are local so
        # it can render the affordance once the user picks an RG / account.
        return {
            "is_local": True,
            "public_access": None,
            "default_action": None,
            "ip_rules": [],
            "caller_ip": None,
            "caller_ip_in_rules": False,
        }

    try:
        _check(subscription_id, _RE_SUB, "subscription_id")
        _check(resource_group, _RE_RG, "resource_group")
        _check(account_name, _RE_STORAGE_ACCOUNT, "account_name")
    except HTTPException:
        return {"is_local": True, "error": "invalid scope parameters"}

    cred = get_credential()
    return read_local_storage_state(cred, subscription_id, resource_group, account_name)


@router.post("/local-debug/open")
def storage_local_debug_open(
    body: dict[str, Any] = Body(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Open the workload Storage account's public network surface to the
    caller's IP. Local-only (refuses inside a Container App).

    The request is the explicit operator confirmation, so the env-var gate
    (``LOCAL_DEBUG_AUTO_OPEN_STORAGE``) is bypassed. The Container-App guard
    is NOT bypassed — see ``ensure_local_storage_access(force=True)``.
    """
    from api.services.storage_public_access import (
        ensure_local_storage_access,
        is_running_locally,
    )

    if not is_running_locally():
        raise HTTPException(
            status_code=403,
            detail="storage local-debug toggle is unavailable in deployed environments",
        )

    sub = body.get("subscription_id", "")
    rg = body.get("resource_group", "")
    account_name = body.get("account_name", "")
    if not all([sub, rg, account_name]):
        raise HTTPException(400, "subscription_id, resource_group, account_name required")
    _check(sub, _RE_SUB, "subscription_id")
    _check(rg, _RE_RG, "resource_group")
    _check(account_name, _RE_STORAGE_ACCOUNT, "account_name")

    cred = get_credential()
    result = ensure_local_storage_access(cred, sub, rg, account_name, force=True)
    LOGGER.info(
        "storage_local_debug_open oid=%s account=%s action=%s",
        caller.object_id,
        account_name,
        result.get("action"),
    )
    if result.get("action") == "failed":
        raise HTTPException(500, f"could not open storage: {result.get('error')}")
    return result
