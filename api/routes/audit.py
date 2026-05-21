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
        from api.services.state_repo import get_state_repo

        repo = get_state_repo()
        # List recent jobs for the caller, then collect their history
        jobs = repo.list_for_owner(caller.object_id, limit=50)
        events: list[dict[str, Any]] = []
        for job in jobs[:20]:  # cap to avoid excessive table queries
            history = repo.get_history(job.job_id, limit=20)
            for h in history:
                if action and h.get("event") != action:
                    continue
                # Sanitise payload_json before it leaves the api boundary:
                # the same blob may contain SAS URLs, bearer tokens, or
                # subscription ids that audit consumers must not see in
                # raw form. The repo layer stores raw blobs for forensic
                # use; redaction is a presentation concern.
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
        # Sanitise the exception message before returning it to the
        # browser: state-repo / Storage SDK errors routinely embed the
        # account URL, request id, and sometimes a SAS query string in
        # the message. The raw text is fine for the server-side log but
        # never the HTTP body.
        LOGGER.warning("audit_log failed: %s", exc)
        return {"events": [], "error": sanitise(str(exc))[:200]}
