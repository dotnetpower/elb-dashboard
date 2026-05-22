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
from typing import Any

from fastapi import APIRouter, Depends

from api.auth import CallerIdentity, require_caller
from api.services import get_credential
from api.services.sanitise import sanitise

LOGGER = logging.getLogger(__name__)

router = APIRouter(tags=["identity"])


def _list_visible_subscriptions() -> tuple[list[dict[str, Any]], str | None]:
    """Best-effort list of subscriptions visible to the api MI / dev az login.

    Returns ``(subscriptions, error)``. On success ``error`` is ``None``;
    on failure ``subscriptions`` is empty and ``error`` is a short sanitised
    string suitable for the SPA to render as a hint.
    """
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
        subs.sort(key=lambda x: (x.get("displayName") or "").lower())
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

