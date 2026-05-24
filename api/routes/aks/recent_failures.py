"""`/api/aks/recent-failed-provisions` route — surfaces the caller's
recently failed `aks_provision` JobState rows so the dashboard can show
a sticky "Last attempt failed" banner that survives both browser
reloads *and* cross-browser sessions (something the FE-only
localStorage fallback in [`lastFailedProvision.ts`] cannot).

Responsibility: HTTP surface only. Queries the JobStateRepository
    in-process (no new repository method) and filters in memory by
    `type=="aks_provision"`, `status=="failed"`, `owner_oid==caller`,
    and a freshness window (default 24h).
Edit boundaries: Keep this route thin. Any new filter/aggregation
    belongs in a dedicated service module that the FE can also
    reuse.
Key entry points: `aks_recent_failed_provisions`.
Risky contracts: Authorization is `owner_oid` only — never expose
    other users' rows even if they were created by the same
    subscription / tenant.
Validation: `uv run pytest -q api/tests/test_aks_recent_failed_provisions.py`.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query

from api.auth import CallerIdentity, require_caller
from api.services.state_repo import get_state_repo

LOGGER = logging.getLogger(__name__)

router = APIRouter()

# Bounded so a noisy / misbehaved client can't request thousands of
# rows in one shot. Real users typically have 0-2 failures in 24h.
_MAX_LIMIT = 20


@router.get("/recent-failed-provisions")
def aks_recent_failed_provisions(
    hours: int = Query(default=24, ge=1, le=168),
    limit: int = Query(default=10, ge=1, le=_MAX_LIMIT),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Return the caller's recently failed `aks_provision` JobState rows."""
    cutoff = (datetime.now(UTC) - timedelta(hours=hours)).isoformat(
        timespec="seconds"
    )
    out: list[dict[str, Any]] = []
    try:
        repo = get_state_repo()
        # `list_for_owner` already enforces owner_oid + the shared-row
        # exception (owner_oid==""). We then filter to provisioning
        # failures within the window. The limit is generous up front
        # because we filter heavily in-process.
        rows = repo.list_for_owner(
            caller.object_id,
            limit=200,
            # We need the payload so we can surface the failed
            # attempt's `region` (the summary select drops it). Limit
            # is bounded at 200 rows so the extra bytes per row stay
            # cheap; the in-process filter then cuts to the small
            # subset of aks_provision failures we actually return.
            include_payload=True,
        )
    except Exception as exc:
        LOGGER.warning(
            "list_for_owner failed for recent-failed-provisions: %s",
            type(exc).__name__,
        )
        return {"jobs": [], "degraded": True}

    for row in rows:
        if row.type != "aks_provision":
            continue
        if row.status != "failed":
            continue
        updated = row.updated_at or row.created_at or ""
        if updated and updated < cutoff:
            continue
        out.append(
            {
                "job_id": row.job_id,
                "task_id": row.task_id,
                "status": row.status,
                "phase": row.phase,
                "error_code": row.error_code,
                "cluster_name": row.cluster_name,
                "region": (row.payload or {}).get("region"),
                "resource_group": row.resource_group,
                "subscription_id": row.subscription_id,
                "created_at": row.created_at,
                "updated_at": row.updated_at,
            }
        )
        if len(out) >= limit:
            break
    out.sort(key=lambda j: j.get("updated_at") or "", reverse=True)
    return {"jobs": out[:limit], "degraded": False}
