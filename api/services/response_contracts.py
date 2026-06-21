"""API response contract builders for long-running control-plane workflows.

Responsibility: API response contract builders for long-running control-plane workflows.
Edit boundaries: Keep this module limited to side-effect-free response shaping helpers.
Key entry points: `build_meta`, `build_target`, `build_operation`, `build_admission`.
Risky contracts: Preserve existing route response fields while adding operation/job/admission
metadata for newer clients.
Validation: `uv run pytest -q api/tests/test_response_contracts.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

AdmissionDecision = Literal["accepted", "would_accept", "would_reject", "rejected"]


def utc_now_iso() -> str:
    """Return a stable UTC timestamp format for API contract metadata."""

    return datetime.now(UTC).isoformat(timespec="seconds")


def build_meta(
    *,
    request_id: str | None = None,
    warnings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build optional response metadata without changing legacy top-level shapes."""

    meta: dict[str, Any] = {"generated_at": utc_now_iso()}
    if request_id:
        meta["request_id"] = request_id
    meta["warnings"] = warnings or []
    return meta


def build_page(
    *,
    limit: int,
    returned: int,
    has_more: bool,
    next_cursor: str | None = None,
) -> dict[str, Any]:
    """Build an OpenAPI-standard pagination envelope for a list response.

    Additive metadata that sits alongside the legacy top-level ``jobs`` array so
    existing clients keep working. ``limit`` is the page size the caller asked
    for, ``returned`` is how many items this page actually carries, and
    ``has_more`` signals whether at least one more item exists beyond this page
    (the list route derives it with a fetch-one-extra probe so it stays honest
    without a server-side ordered index). ``next_cursor`` carries the keyset of
    the last emitted row for true cursor pagination, served by the BLAST jobs
    list route off the time-ordered index (#50/#51) when the index flag is on;
    it is omitted while None so the shape stays clean for callers that do not
    paginate (scoped listings and the flag-off path).
    """

    page: dict[str, Any] = {
        "limit": limit,
        "returned": returned,
        "has_more": has_more,
    }
    if next_cursor is not None:
        page["next_cursor"] = next_cursor
    return page


def build_target(
    *,
    resource_type: str,
    job_id: str,
    job_id_kind: str = "dashboard",
    dashboard_job_id: str | None = None,
    openapi_job_id: str | None = None,
    links: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Describe the domain resource affected by an operation.

    `job_id` remains the caller-facing canonical id for legacy clients, while
    `dashboard_job_id` and `openapi_job_id` make cross-plane id usage explicit.
    """

    target: dict[str, Any] = {
        "resource_type": resource_type,
        "job_id": job_id,
        "job_id_kind": job_id_kind,
        "dashboard_job_id": dashboard_job_id if dashboard_job_id is not None else job_id,
        "openapi_job_id": openapi_job_id,
    }
    if links:
        target["links"] = links
    return target


def build_operation(
    *,
    operation_id: str,
    operation_type: str,
    state: str = "queued",
    accepted_at: str | None = None,
    poll_after_seconds: int = 5,
    links: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Describe an asynchronous control-plane operation accepted by the API."""

    return {
        "operation_id": operation_id,
        "operation_type": operation_type,
        "state": state,
        "accepted_at": accepted_at or utc_now_iso(),
        "poll_after_seconds": poll_after_seconds,
        "links": links or {},
    }


def build_admission(
    *,
    decision: AdmissionDecision,
    reason: str,
    basis: str = "current_control_plane_snapshot",
    snapshot_at: str | None = None,
    queue: dict[str, Any] | None = None,
    capacity: dict[str, Any] | None = None,
    warnings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Describe an admission decision as a point-in-time snapshot, not a guarantee."""

    return {
        "decision": decision,
        "reason": reason,
        "basis": basis,
        "snapshot_at": snapshot_at or utc_now_iso(),
        "queue": queue or {"state": decision, "depth_bucket": "unknown"},
        "capacity": capacity or {"classification": "not_evaluated"},
        "warnings": warnings or [],
    }


def request_id_from_scope(scope: Any) -> str | None:
    """Best-effort extraction of the RequestIdMiddleware id from FastAPI request scope."""

    state = getattr(scope, "state", None)
    request_id = getattr(state, "request_id", None)
    return str(request_id) if request_id else None
