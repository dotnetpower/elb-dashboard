"""/api/audit/log`` - best-effort read from the jobhistory table.

Responsibility: /api/audit/log`` - best-effort read from the jobhistory table
Edit boundaries: Keep HTTP validation and response shaping here; move cloud/data-plane work into
services or tasks.
Key entry points: `audit_log`
Risky contracts: Every non-health `/api/*` route must enforce `require_caller` or an equivalent
auth gate.
Validation: `uv run pytest -q api/tests/test_route_contracts.py`.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query

from api.auth import CallerIdentity, require_caller
from api.services.sanitise import sanitise

LOGGER = logging.getLogger(__name__)

audit_router = APIRouter(prefix="/api/audit", tags=["audit"])


@audit_router.get("/log")
def audit_log(
    limit: int = Query(default=200, ge=1, le=1000),
    action: str = Query(default=""),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Return recent audit events from the jobhistory table."""
    try:
        from api.services.state.repository import get_state_repo

        repo = get_state_repo()
        # 1) List recent jobs for the caller (one Table query). The audit
        # view never reads payload_json, only the job_id/type fields, so we
        # skip pulling the per-row payload blob.
        jobs = repo.list_for_owner(caller.object_id, limit=50, include_payload=False)
        job_window = jobs[:20]
        if not job_window:
            return {"events": []}
        # 2) Bulk-fetch history for those jobs in ONE Table query — the
        # previous loop issued one call per job (N+1).
        history_by_job = repo.get_history_for_jobs(
            [job.job_id for job in job_window],
            per_job_limit=20,
        )
        # 3) Flatten + filter + sanitise. ``payload_json`` may contain SAS
        # URLs, bearer tokens, or subscription ids; the repo stores raw
        # blobs for forensic use and redaction is a presentation concern.
        events: list[dict[str, Any]] = []
        for job in job_window:
            for h in history_by_job.get(job.job_id, ()):
                if action and h.get("event") != action:
                    continue
                events.append(
                    {
                        "job_id": job.job_id,
                        "job_type": job.type,
                        "event": h.get("event", ""),
                        "ts": h.get("ts", ""),
                        "payload": sanitise(str(h.get("payload_json", ""))),
                    }
                )
                if len(events) >= limit:
                    break
            if len(events) >= limit:
                break
        events.sort(key=lambda e: e.get("ts", ""), reverse=True)
        return {"events": events[:limit]}
    except Exception as exc:
        # Sanitise the exception message before BOTH the server-side log
        # and the response body. Azure SDK / Table errors routinely embed
        # the account URL, request id, and sometimes SAS-style query
        # strings; the server log is not a public surface but persists in
        # Log Analytics and any operator query can leak those values into
        # workbook screenshots / tickets.
        safe_exc = sanitise(str(exc))[:300]
        LOGGER.warning("audit_log failed: %s", safe_exc)
        return {"events": [], "error": safe_exc[:200]}
