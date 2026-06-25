"""``/api/notifications`` — in-app job notification feed + per-user seen marker.

Responsibility: HTTP surface for the notification center. Validates the caller,
delegates feed assembly and marker writes to ``api.services.notifications``, and
shapes the JSON response.
Edit boundaries: Keep HTTP validation and response shaping here; all jobstate
reads and marker storage live in ``api.services.notifications``.
Key entry points: ``list_notifications``, ``mark_seen``.
Risky contracts: Every non-health ``/api/*`` route must enforce ``require_caller``.
Validation: ``uv run pytest -q api/tests/test_notifications.py``.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query

from api.auth import CallerIdentity, require_caller

LOGGER = logging.getLogger(__name__)

notifications_router = APIRouter(prefix="/api/notifications", tags=["notifications"])


@notifications_router.get("")
def list_notifications(
    limit: int = Query(default=50, ge=1, le=100),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Return the caller's recent terminal-job notifications + unread count."""
    from api.services.notifications import build_notifications

    return build_notifications(caller.object_id, limit=limit)


@notifications_router.post("/seen")
def mark_seen(
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Mark every current notification as seen (advance the seen marker)."""
    from api.services.notifications import mark_all_seen

    return mark_all_seen(caller.object_id)
