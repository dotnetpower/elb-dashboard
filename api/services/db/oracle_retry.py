"""Durable retry policy for automatic DB order-oracle builds.

Responsibility: Classify whether automation may retry and record queued,
    successful, or failed outcomes in the oracle automation control document.
Edit boundaries: Pure retry math and calls to the durable automation state;
    dispatch, task execution, preferences, and UI remain in owning modules.
Key entry points: `automation_retry_allowed`, `record_automation_dispatch`,
    `record_automation_success`, `record_automation_failure`,
    `reset_automation_retry`.
Risky contracts: Expected readiness blockers do not consume retry budget;
    failures back off 5m/30m/2h, the third failure requires manual reset, and
    delayed automatic success cannot reset a newer run's retry state.
Validation: `uv run pytest -q api/tests/test_oracle_retry.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

_RETRY_DELAYS_SECONDS = (5 * 60, 30 * 60, 2 * 60 * 60)
_MAX_FAILURES = len(_RETRY_DELAYS_SECONDS)


def _parse_time(value: object) -> datetime | None:
    text = str(value or "")
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    except ValueError:
        return None


def automation_retry_allowed(
    state: dict[str, Any] | None,
    *,
    now: datetime | None = None,
) -> tuple[bool, str]:
    if not isinstance(state, dict):
        return True, "no_state"
    if bool(state.get("retry_exhausted")):
        return False, "retry_exhausted"
    retry_at = _parse_time(state.get("next_retry_at"))
    current = now or datetime.now(UTC)
    if retry_at is not None and current < retry_at:
        return False, "retry_backoff"
    return True, "ready"


def record_automation_dispatch(
    container: Any,
    *,
    db_name: str,
    run_id: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    from api.services.db.oracle_state import update_oracle_automation

    current = now or datetime.now(UTC)
    return update_oracle_automation(
        container,
        db_name=db_name,
        updates={
            "status": "queued",
            "last_run_id": run_id,
            "last_error_code": "",
            "blocked_reason": "",
            "updated_at": current.isoformat(timespec="seconds"),
        },
    )


def record_automation_success(
    container: Any,
    *,
    db_name: str,
    run_id: str,
    now: datetime | None = None,
    require_current_run: bool = False,
) -> dict[str, Any]:
    """Record success, optionally only for the currently tracked run."""
    from api.services.db.oracle_state import mutate_oracle_automation

    current = now or datetime.now(UTC)

    def _success_updates(previous: dict[str, Any]) -> dict[str, Any]:
        if require_current_run and str(previous.get("last_run_id") or "") != run_id:
            return {}
        return {
            "status": "ready",
            "failure_count": 0,
            "retry_exhausted": False,
            "next_retry_at": "",
            "last_run_id": run_id,
            "last_error_code": "",
            "blocked_reason": "",
            "updated_at": current.isoformat(timespec="seconds"),
        }

    return mutate_oracle_automation(
        container,
        db_name=db_name,
        mutator=_success_updates,
    )


def reset_automation_retry(
    container: Any,
    *,
    db_name: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Clear an exhausted/backoff budget after an authorized user retry."""
    from api.services.db.oracle_state import update_oracle_automation

    current = now or datetime.now(UTC)
    return update_oracle_automation(
        container,
        db_name=db_name,
        updates={
            "status": "idle",
            "failure_count": 0,
            "retry_exhausted": False,
            "next_retry_at": "",
            "last_error_code": "",
            "blocked_reason": "",
            "updated_at": current.isoformat(timespec="seconds"),
        },
    )


def record_automation_failure(
    container: Any,
    *,
    db_name: str,
    run_id: str,
    error_code: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    from api.services.db.oracle_state import mutate_oracle_automation

    current = now or datetime.now(UTC)

    def _failure_updates(previous: dict[str, Any]) -> dict[str, Any]:
        if previous.get("status") == "failed" and str(previous.get("last_run_id") or "") == run_id:
            return {}
        failures = min(int(previous.get("failure_count") or 0) + 1, _MAX_FAILURES)
        exhausted = failures >= _MAX_FAILURES
        delay = _RETRY_DELAYS_SECONDS[failures - 1]
        return {
            "status": "failed",
            "failure_count": failures,
            "retry_exhausted": exhausted,
            "next_retry_at": (
                ""
                if exhausted
                else (current + timedelta(seconds=delay)).isoformat(timespec="seconds")
            ),
            "last_run_id": run_id,
            "last_error_code": error_code,
            "blocked_reason": "",
            "updated_at": current.isoformat(timespec="seconds"),
        }

    exhaustion_transition = {"value": False}

    def _tracked_failure_updates(previous: dict[str, Any]) -> dict[str, Any]:
        updates = _failure_updates(previous)
        exhaustion_transition["value"] = (
            bool(updates.get("retry_exhausted"))
            and int(previous.get("failure_count") or 0) < _MAX_FAILURES
        )
        return updates

    result = mutate_oracle_automation(
        container,
        db_name=db_name,
        mutator=_tracked_failure_updates,
    )
    if exhaustion_transition["value"]:
        from api.services.feature_events import record_feature_event

        record_feature_event(
            "oracle_retry_exhausted",
            status="failed",
            database=db_name,
            run_id=run_id,
            error_code=error_code,
            failure_count=_MAX_FAILURES,
        )
    return result


__all__ = [
    "automation_retry_allowed",
    "record_automation_dispatch",
    "record_automation_failure",
    "record_automation_success",
    "reset_automation_retry",
]
