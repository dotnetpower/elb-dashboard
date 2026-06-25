"""Tests for BLAST auto-retry decision logic (eligibility, restore, backoff).

Responsibility: Lock the pure ``evaluate`` decision tree, ``restore_submit_kwargs``
required-field guard, the attempt counter / quarantine transitions, and backoff.
Edit boundaries: Test-only; no Azure, no Celery. Uses a fake JobState dataclass.
Key entry points: pytest test functions.
Risky contracts: ``evaluate`` must never return ``retry`` for a non-transient or
backoff-pending job; ``restore_submit_kwargs`` must return ``None`` on any missing
required field.
Validation: ``uv run pytest -q api/tests/test_blast_auto_retry.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest
from api.services.blast import auto_retry


@dataclass
class FakeState:
    job_id: str = "job-1"
    status: str = "failed"
    phase: str = "submitting"
    error_code: str = "terminal_az_login_failed"
    updated_at: str = "2026-06-25T00:00:00+00:00"
    parent_job_id: str | None = None
    subscription_id: str = "sub-1"
    resource_group: str = "rg-1"
    cluster_name: str = "clu-1"
    storage_account: str = "st1"
    program: str = "blastn"
    db: str = "nt"
    owner_oid: str = "oid-1"
    tenant_id: str = "tid-1"
    submission_source: str = "dashboard"
    payload: dict[str, Any] = field(
        default_factory=lambda: {"query_file": "q.fa", "options": {"batch_len": 5000}}
    )


_LATER = datetime(2027, 1, 1, tzinfo=UTC)  # far past any backoff window


def test_restore_submit_kwargs_complete() -> None:
    kwargs = auto_retry.restore_submit_kwargs(FakeState())
    assert kwargs is not None
    assert kwargs["subscription_id"] == "sub-1"
    assert kwargs["database"] == "nt"
    assert kwargs["query_file"] == "q.fa"
    assert kwargs["options"] == {"batch_len": 5000}
    assert kwargs["caller_oid"] == "oid-1"


@pytest.mark.parametrize("missing", ["cluster_name", "storage_account", "program"])
def test_restore_returns_none_when_required_column_missing(missing: str) -> None:
    state = FakeState()
    setattr(state, missing, "")
    assert auto_retry.restore_submit_kwargs(state) is None


def test_restore_returns_none_when_query_file_missing() -> None:
    state = FakeState(payload={"options": {}})
    assert auto_retry.restore_submit_kwargs(state) is None


def test_read_meta_empty_payload() -> None:
    meta = auto_retry.read_meta({})
    assert meta.count == 0
    assert meta.quarantined is False


def test_read_meta_existing() -> None:
    meta = auto_retry.read_meta(
        {"auto_retry": {"count": 2, "quarantined": True, "last_error_code": "x"}}
    )
    assert meta.count == 2
    assert meta.quarantined is True


def test_backoff_is_exponential_and_capped() -> None:
    assert auto_retry.backoff_seconds(0) == 60
    assert auto_retry.backoff_seconds(1) == 120
    assert auto_retry.backoff_seconds(2) == 240
    assert auto_retry.backoff_seconds(100) == 1800  # capped


def test_evaluate_skip_not_failed() -> None:
    assert auto_retry.evaluate(FakeState(status="running"), now=_LATER).action == "skip"


def test_evaluate_skip_split_child() -> None:
    d = auto_retry.evaluate(FakeState(parent_job_id="parent"), now=_LATER)
    assert d.action == "skip"
    assert d.reason == "split_child"


def test_evaluate_skip_non_transient() -> None:
    d = auto_retry.evaluate(FakeState(error_code="blast_search_failed"), now=_LATER)
    assert d.action == "skip"
    assert d.reason.startswith("not_auto_retryable")


def test_evaluate_skip_external_submission_source() -> None:
    d = auto_retry.evaluate(FakeState(submission_source="servicebus"), now=_LATER)
    assert d.action == "skip"
    assert d.reason == "external_origin"


def test_evaluate_skip_external_payload() -> None:
    state = FakeState(payload={"query_file": "q.fa", "external": {"job_id": "x"}})
    d = auto_retry.evaluate(state, now=_LATER)
    assert d.action == "skip"
    assert d.reason == "external_origin"


def test_evaluate_retry_due() -> None:
    d = auto_retry.evaluate(FakeState(), now=_LATER)
    assert d.action == "retry"
    assert d.kwargs is not None
    assert d.next_meta is not None
    assert d.next_meta.count == 1


def test_evaluate_backoff_not_elapsed() -> None:
    just_failed = datetime.now(UTC).isoformat(timespec="seconds")
    d = auto_retry.evaluate(FakeState(updated_at=just_failed), now=datetime.now(UTC))
    assert d.action == "skip"
    assert d.reason == "backoff_not_elapsed"


def test_evaluate_quarantine_budget_exhausted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BLAST_AUTO_RETRY_MAX", "2")
    state = FakeState(
        payload={
            "query_file": "q.fa",
            "auto_retry": {"count": 2, "quarantined": False},
        }
    )
    d = auto_retry.evaluate(state, now=_LATER)
    assert d.action == "quarantine"
    assert d.next_meta is not None
    assert d.next_meta.quarantined is True


def test_evaluate_skip_already_quarantined() -> None:
    state = FakeState(payload={"query_file": "q.fa", "auto_retry": {"quarantined": True}})
    d = auto_retry.evaluate(state, now=_LATER)
    assert d.action == "skip"
    assert d.reason == "already_quarantined"


def test_evaluate_quarantine_when_unrestorable() -> None:
    # Past backoff + transient + budget left, but query_file missing => quarantine.
    state = FakeState(payload={"options": {}})
    d = auto_retry.evaluate(state, now=_LATER)
    assert d.action == "quarantine"
    assert d.reason == "submit_kwargs_unrestorable"


def test_merge_meta_preserves_other_keys() -> None:
    meta = auto_retry.AutoRetryMeta(count=1, max=2)
    merged = auto_retry.merge_meta_into_payload({"_progress": {"x": 1}}, meta)
    assert merged["_progress"] == {"x": 1}
    assert merged["auto_retry"]["count"] == 1


def test_gate_and_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BLAST_AUTO_RETRY_ENABLED", raising=False)
    assert auto_retry.auto_retry_enabled() is False
    monkeypatch.setenv("BLAST_AUTO_RETRY_ENABLED", "true")
    assert auto_retry.auto_retry_enabled() is True
    monkeypatch.setenv("BLAST_AUTO_RETRY_SWEEP_LIMIT", "9")
    assert auto_retry.sweep_limit() == 9
    monkeypatch.setenv("BLAST_AUTO_RETRY_SCAN_LIMIT", "42")
    assert auto_retry.max_scan() == 42
    monkeypatch.setenv("BLAST_AUTO_RETRY_SCAN_LIMIT", "999999")
    assert auto_retry.max_scan() == 1000  # clamped
