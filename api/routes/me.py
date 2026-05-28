"""Caller identity endpoint. Returns the validated token's `oid`/`tid`/`upn`.

Responsibility: Caller identity endpoint. Returns the validated token's identity claims and
the list of subscriptions visible to the api sidecar's shared managed identity, so the SPA
can render the right Subscription picker on first load and detect stale workspace settings
(saved subscription that the current credential cannot see).
Edit boundaries: Keep HTTP validation and response shaping here; move cloud/data-plane work into
services or tasks.
Key entry points: `me`
Risky contracts: Every non-health `/api/*` route must enforce `require_caller` or an equivalent
auth gate. The `subscriptions` field is best-effort: when ARM listing fails the response keeps
the identity claims and surfaces a non-fatal `subscriptions_error` string. SPA contract — the
`WorkspaceDiagnosticsBanner` branches on whether the saved subscription is in this list.
Validation: `uv run pytest -q api/tests/test_route_contracts.py api/tests/test_me_route.py`.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

from fastapi import APIRouter, Depends, Query

from api.auth import CallerIdentity, require_caller
from api.services import get_credential
from api.services.me_permissions import compute_caller_permissions
from api.services.sanitise import sanitise

LOGGER = logging.getLogger(__name__)

router = APIRouter(tags=["identity"])

_SUBSCRIPTION_CACHE_TTL_SECONDS = float(os.environ.get("ME_SUBSCRIPTIONS_TTL_SECONDS", "60"))
_SUBSCRIPTION_LIST_LIMIT = max(1, int(os.environ.get("ME_SUBSCRIPTIONS_LIST_LIMIT", "100")))
_SUBSCRIPTION_CACHE_LOCK = threading.Lock()
_SUBSCRIPTION_CACHE: tuple[float, tuple[list[dict[str, Any]], str | None]] | None = None


def reset_subscription_cache_for_tests() -> None:
    """Clear the process-local subscription cache used by `/api/me` tests."""
    global _SUBSCRIPTION_CACHE
    with _SUBSCRIPTION_CACHE_LOCK:
        _SUBSCRIPTION_CACHE = None


def _list_visible_subscriptions() -> tuple[list[dict[str, Any]], str | None]:
    """Best-effort list of subscriptions visible to the api MI / dev az login.

    Returns ``(subscriptions, error)``. On success ``error`` is ``None``;
    on failure ``subscriptions`` is empty and ``error`` is a short sanitised
    string suitable for the SPA to render as a hint.
    """
    global _SUBSCRIPTION_CACHE
    now = time.monotonic()
    with _SUBSCRIPTION_CACHE_LOCK:
        if _SUBSCRIPTION_CACHE is not None and _SUBSCRIPTION_CACHE[0] > now:
            subs, error = _SUBSCRIPTION_CACHE[1]
            return [dict(item) for item in subs], error

    try:
        from azure.mgmt.resource import SubscriptionClient
    except Exception as exc:  # pragma: no cover - import guard
        return [], f"subscription_client_unavailable: {type(exc).__name__}"

    try:
        cred = get_credential()
        client = SubscriptionClient(cred)
        subs: list[dict[str, Any]] = []
        for s in client.subscriptions.list():
            state = s.state
            subs.append(
                {
                    "subscriptionId": s.subscription_id,
                    "displayName": s.display_name,
                    "tenantId": s.tenant_id,
                    "state": state.value if hasattr(state, "value") else str(state or "Unknown"),
                }
            )
            if len(subs) >= _SUBSCRIPTION_LIST_LIMIT:
                break
        subs.sort(key=lambda x: (x.get("displayName") or "").lower())
        with _SUBSCRIPTION_CACHE_LOCK:
            _SUBSCRIPTION_CACHE = (
                time.monotonic() + _SUBSCRIPTION_CACHE_TTL_SECONDS,
                ([dict(item) for item in subs], None),
            )
        return subs, None
    except Exception as exc:
        LOGGER.warning(
            "me.list_visible_subscriptions failed: %s: %s",
            type(exc).__name__,
            sanitise(str(exc)),
            exc_info=True,
        )
        return [], f"{type(exc).__name__}: {sanitise(str(exc))[:160]}"


@router.get("/me")
def me(caller: CallerIdentity = Depends(require_caller)) -> dict[str, Any]:
    """Return the validated caller's identity claims + visible subscriptions.

    Mirrors the Function App's `GET /api/me` for the identity fields and
    augments the response with the list of subscriptions the api sidecar's
    managed identity (or, in local dev, the developer's az CLI session)
    can see. The SPA uses this to:

      * pre-populate the Subscription picker without a second round-trip,
      * detect when a saved `subscriptionId` in `localStorage` is not in the
        visible list (typical when the developer switched az profiles) and
        surface the workspace diagnostics banner.
    """
    subscriptions, error = _list_visible_subscriptions()
    body: dict[str, Any] = {
        "object_id": caller.object_id,
        "tenant_id": caller.tenant_id,
        "upn": caller.upn,
        "subscriptions": subscriptions,
    }
    if error:
        body["subscriptions_error"] = error
    return body


@router.get("/me/permissions")
def me_permissions(
    subscription_id: str = Query(..., description="Azure subscription id"),
    resource_group: str | None = Query(
        None, description="Optional resource group to scope the check"
    ),
    cluster_name: str | None = Query(
        None, description="Optional AKS cluster name to scope the check"
    ),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Return the calling user's effective RBAC capabilities at a scope.

    The SPA uses this to disable Start/Stop/Delete/Submit/Build buttons
    when the signed-in user lacks the underlying Azure role at the
    requested scope, and to render a tooltip explaining which role
    they currently hold vs which role would be needed.

    Important: this is a UX affordance, NOT a security boundary. The
    real authorization check runs at submit time inside ARM / Storage
    against the worker's managed identity (which is the canonical
    enforcement point). Enumeration failures degrade open
    (``degraded=true`` + all capabilities ``true``) so a transient
    ARM hiccup never locks the operator out.
    """
    perms = compute_caller_permissions(
        get_credential(),
        caller_oid=caller.object_id,
        subscription_id=subscription_id,
        resource_group=resource_group,
        cluster_name=cluster_name,
    )
    return perms.to_dict()

