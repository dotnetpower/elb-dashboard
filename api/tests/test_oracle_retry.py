"""Tests for automatic oracle retry policy.

Responsibility: Verify bounded backoff, third-failure exhaustion, time gates,
    and success reset without cloud access.
Edit boundaries: Mock durable automation writes; dispatch/task behavior is
    covered elsewhere.
Key entry points: `test_failure_backoff_and_exhaustion`,
    `test_retry_allowed_honours_backoff`, `test_success_resets_budget`,
    `test_stale_success_does_not_reset_newer_run`.
Risky contracts: Automatic failure loops must stop after three attempts while
    manual success can restore normal automation and delayed automatic success
    cannot overwrite a newer run.
Validation: `uv run pytest -q api/tests/test_oracle_retry.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from api.services.db import oracle_retry


def test_retry_allowed_honours_backoff_and_exhaustion() -> None:
    now = datetime(2026, 8, 27, tzinfo=UTC)

    assert oracle_retry.automation_retry_allowed(None, now=now) == (
        True,
        "no_state",
    )
    assert oracle_retry.automation_retry_allowed(
        {"next_retry_at": (now + timedelta(minutes=1)).isoformat()}, now=now
    ) == (False, "retry_backoff")
    assert oracle_retry.automation_retry_allowed({"retry_exhausted": True}, now=now) == (
        False,
        "retry_exhausted",
    )


def test_failure_backoff_and_exhaustion(monkeypatch: pytest.MonkeyPatch) -> None:
    state = {"failure_count": 0}

    def _mutate(_container, *, db_name, mutator):
        del db_name
        updates = mutator(dict(state))
        state.update(updates)
        return dict(state)

    monkeypatch.setattr("api.services.db.oracle_state.mutate_oracle_automation", _mutate)
    events = []
    monkeypatch.setattr(
        "api.services.feature_events.record_feature_event",
        lambda event, **kwargs: events.append((event, kwargs)),
    )
    now = datetime(2026, 8, 27, tzinfo=UTC)

    first = oracle_retry.record_automation_failure(
        object(), db_name="core_nt", run_id="run-1", error_code="failed", now=now
    )
    second = oracle_retry.record_automation_failure(
        object(), db_name="core_nt", run_id="run-2", error_code="failed", now=now
    )
    third = oracle_retry.record_automation_failure(
        object(), db_name="core_nt", run_id="run-3", error_code="failed", now=now
    )

    assert first["next_retry_at"] == (now + timedelta(minutes=5)).isoformat(timespec="seconds")
    assert second["next_retry_at"] == (now + timedelta(minutes=30)).isoformat(timespec="seconds")
    assert third["retry_exhausted"] is True
    assert third["next_retry_at"] == ""
    assert [event for event, _kwargs in events] == ["oracle_retry_exhausted"]


def test_success_resets_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    state = {"status": "failed", "failure_count": 2, "last_run_id": "run-ready"}

    def _mutate(_container, *, db_name, mutator):
        del db_name
        state.update(mutator(dict(state)))
        return dict(state)

    monkeypatch.setattr(
        "api.services.db.oracle_state.mutate_oracle_automation",
        _mutate,
    )

    result = oracle_retry.record_automation_success(
        object(),
        db_name="core_nt",
        run_id="run-ready",
        require_current_run=True,
    )

    assert result["failure_count"] == 0
    assert result["retry_exhausted"] is False
    assert result["last_run_id"] == "run-ready"
    assert result["blocked_reason"] == ""


def test_stale_success_does_not_reset_newer_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = {"status": "failed", "failure_count": 2, "last_run_id": "run-new"}

    def _mutate(_container, *, db_name, mutator):
        del db_name
        state.update(mutator(dict(state)))
        return dict(state)

    monkeypatch.setattr(
        "api.services.db.oracle_state.mutate_oracle_automation",
        _mutate,
    )

    result = oracle_retry.record_automation_success(
        object(),
        db_name="core_nt",
        run_id="run-old",
        require_current_run=True,
    )

    assert result == {
        "status": "failed",
        "failure_count": 2,
        "last_run_id": "run-new",
    }


def test_explicit_retry_clears_error_and_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = {}
    monkeypatch.setattr(
        "api.services.db.oracle_state.update_oracle_automation",
        lambda _container, *, db_name, updates: captured.update(updates) or updates,
    )

    result = oracle_retry.reset_automation_retry(object(), db_name="core_nt")

    assert result["status"] == "idle"
    assert result["failure_count"] == 0
    assert result["retry_exhausted"] is False
    assert result["next_retry_at"] == ""
    assert result["blocked_reason"] == ""


def test_same_run_failure_consumes_budget_only_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = {"failure_count": 0}

    def _mutate(_container, *, db_name, mutator):
        del db_name
        state.update(mutator(dict(state)))
        return dict(state)

    monkeypatch.setattr("api.services.db.oracle_state.mutate_oracle_automation", _mutate)

    first = oracle_retry.record_automation_failure(
        object(), db_name="core_nt", run_id="same-run", error_code="failed"
    )
    duplicate = oracle_retry.record_automation_failure(
        object(), db_name="core_nt", run_id="same-run", error_code="failed"
    )

    assert first["failure_count"] == 1
    assert duplicate["failure_count"] == 1
