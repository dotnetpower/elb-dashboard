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
from api.services.access_review import (
    dashboard_identity_principal_id,
    review_resource_group_access,
)
from api.services.dashboard_access import require_dashboard_access
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
        from api.services.azure_clients import subscription_client
    except Exception as exc:  # pragma: no cover - import guard
        return [], f"subscription_client_unavailable: {type(exc).__name__}"

    try:
        cred = get_credential()
        client = subscription_client(cred)
        subs: list[dict[str, Any]] = []
        truncated = False
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
                # ARM returned more subscriptions than the cap. Stop enumerating
                # (the iterator can be very large in enterprise tenants) but flag
                # it so the SPA can warn the user that the picker is incomplete
                # instead of silently hiding subscriptions past the cap.
                truncated = True
                break
        subs.sort(key=lambda x: (x.get("displayName") or "").lower())
        error = "subscriptions_truncated" if truncated else None
        with _SUBSCRIPTION_CACHE_LOCK:
            _SUBSCRIPTION_CACHE = (
                time.monotonic() + _SUBSCRIPTION_CACHE_TTL_SECONDS,
                ([dict(item) for item in subs], error),
            )
        return subs, error
    except Exception as exc:
        LOGGER.warning(
            "me.list_visible_subscriptions failed: %s: %s",
            type(exc).__name__,
            sanitise(str(exc)),
            exc_info=True,
        )
        return [], f"{type(exc).__name__}: {sanitise(str(exc))[:160]}"


@router.get("/me")
def me(caller: CallerIdentity = Depends(require_dashboard_access)) -> dict[str, Any]:
    """Return the validated caller's identity claims + visible subscriptions.

    This is the SPA's identity bootstrap, so it carries the optional entry
    gate (`require_dashboard_access`). When `ENFORCE_DASHBOARD_RBAC=true`, a
    tenant member with no read RBAC on the dashboard scope gets a 403
    (`dashboard_access_denied`) here and the SPA renders an access-denied
    screen instead of a half-broken dashboard. Default OFF preserves the
    legacy behaviour. The `/me/permissions` and `/me/access-review` routes
    below intentionally keep the plain `require_caller` gate so a *blocked*
    caller can still inspect why they were denied.

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


@router.get("/me/access-review")
def me_access_review(
    subscription_id: str = Query(..., description="Azure subscription id"),
    resource_group: list[str] = Query(
        default=[],
        description=(
            "Resource group(s) to review. Repeat the query parameter to "
            "review several at once (e.g. the dashboard RG and the cluster RG)."
        ),
    ),
    target: str = Query(
        default="me",
        description=(
            "Whose access to review: 'me' (the signed-in caller, default) or "
            "'dashboard' (the shared managed identity the Container App runs "
            "as). The dashboard identity is what actually performs ARM / "
            "Storage writes, so its missing role is the usual root cause of a "
            "tenant onboarding failure."
        ),
    ),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Reproduce the portal \"View my access\" per resource group.

    Lists a principal's effective Azure role assignments (direct plus
    Entra-group-inherited) grouped by each requested resource group, with an
    inheritance flag so the SPA can render an IAM-style table when diagnosing
    why an action fails in a freshly-onboarded tenant. ``target`` selects the
    signed-in caller or the dashboard managed identity.

    Unlike ``/me/permissions``, this surface does NOT degrade open: when
    role enumeration fails (the principal likely lacks
    ``Microsoft.Authorization/roleAssignments/read``) the affected groups
    are returned with ``degraded=true`` and an explicit reason, because a
    fabricated \"you have access\" would defeat the diagnostic.
    """
    if target == "dashboard":
        principal_oid = dashboard_identity_principal_id()
        principal_kind = "dashboard_identity"
    else:
        principal_oid = caller.object_id
        principal_kind = "user"

    review = review_resource_group_access(
        get_credential(),
        principal_oid=principal_oid,
        subscription_id=subscription_id,
        resource_groups=list(resource_group),
        principal_kind=principal_kind,
    )
    return review.to_dict()

