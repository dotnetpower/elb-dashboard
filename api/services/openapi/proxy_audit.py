"""Forensic audit trail for state-changing OpenAPI proxy calls.

Module summary: The ``/api/aks/openapi/proxy`` route auto-injects the admin
``X-ELB-API-Token`` and forwards browser "Try it" calls to the ``elb-openapi``
pod. Read-only GETs are noise, but POST/PUT/PATCH/DELETE drive privileged
state changes (e.g. ``POST /v1/jobs`` submits a BLAST workload) under the
shared admin token. Because the dashboard validates tenant membership but
does not — and, with OBO flows forbidden by charter §12, cannot — enforce a
per-caller Azure RBAC gate, any authenticated tenant member (including a
subscription Reader) can drive execution through the admin token. This helper
records WHICH caller drove each state-changing call so there is a forensic
trail even though the auth layer cannot block the action.

Responsibility: Append one best-effort audit ``JobState`` row per
    state-changing OpenAPI proxy call. Pure side-effect helper.
Edit boundaries: Never block or fail the proxy path; never log or store the
    admin token value. Callers decide which methods are audited (only the
    mutating verbs — GET polling must stay out of the audit log).
Key entry points: ``record_openapi_proxy_exec``.
Risky contracts: Mirrors ``_record_self_heal_audit``'s ``JobState`` shape so
    the existing ``/api/audit/log`` SPA surface and ``list_for_owner`` query
    pick it up without a schema change. ``owner_oid`` defaults to ``system``
    when no caller is available.
Validation: ``uv run pytest -q api/tests/test_openapi_proxy_audit.py``.
"""

from __future__ import annotations

import logging
from typing import Any

LOGGER = logging.getLogger("api.services.openapi.proxy_audit")

# Cap the recorded target path so a pathological query string cannot bloat the
# audit table. OpenAPI service paths are short; 512 chars is generous.
_MAX_PATH_LEN = 512

# Verbs that mutate state on the elb-openapi service. GET / HEAD / OPTIONS are
# intentionally excluded so dashboard polling does not flood the audit log.
_STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def is_state_changing_method(method: str) -> bool:
    """Return True when ``method`` mutates state and should be audited."""
    return method.upper() in _STATE_CHANGING_METHODS


def record_openapi_proxy_exec(
    *,
    method: str,
    target_path: str,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    caller_oid: str = "",
    tenant_id: str = "",
) -> None:
    """Append an audit ``JobState`` row for a state-changing proxy call.

    Best-effort by design: the audit append must never block or fail the
    proxy path. Mirrors ``_record_self_heal_audit`` so the existing
    ``/api/audit/log`` SPA surface picks the event up automatically.

    The synthetic ``job_id`` (``openapi-proxy:<METHOD>:<cluster>:<ulid>``)
    is prefixed so these rows group separately from BLAST / warmup / DB-ops
    jobs without a ``JobState`` schema change.

    The admin token value is NEVER passed here — only the caller identity,
    HTTP method, and OpenAPI service path. This keeps the row safe to render
    in the SPA and to ship to Log Analytics.
    """
    try:
        import uuid
        from datetime import UTC, datetime

        from api.services.state.job_state import JobState
        from api.services.state_repo import get_state_repo

        now = datetime.now(UTC).isoformat(timespec="seconds")
        verb = method.upper()
        safe_path = (target_path or "")[:_MAX_PATH_LEN]
        job_id = f"openapi-proxy:{verb}:{cluster_name or 'unknown'}:{uuid.uuid4().hex[:12]}"
        payload: dict[str, Any] = {
            "event": "openapi_proxy_exec",
            "method": verb,
            "target_path": safe_path,
            "subscription_id": subscription_id,
            "resource_group": resource_group,
            "cluster_name": cluster_name,
            "ts": now,
        }
        repo = get_state_repo()
        repo.create(
            JobState(
                job_id=job_id,
                type="openapi_proxy_exec",
                status="completed",
                phase="requested",
                owner_oid=caller_oid or "system",
                tenant_id=tenant_id or "",
                created_at=now,
                updated_at=now,
                payload=payload,
            )
        )
        repo.append_history(job_id, "openapi_proxy_exec", payload)
    except Exception as exc:
        # Audit append is best-effort — never block the proxy on it.
        LOGGER.warning("openapi proxy exec audit append skipped: %s", type(exc).__name__)
