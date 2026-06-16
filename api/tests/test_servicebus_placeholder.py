"""Tests for the Service Bus send-time ``queued`` placeholder jobstate rows.

Responsibility: Verify ``create_queued_placeholder`` writes a correlation-id-keyed
    ``queued`` row (visible the instant a request is enqueued) with the
    ``enqueued`` message-flow stage, that ``supersede_placeholder`` soft-deletes
    it once the real drained row exists (and only when it is actually a
    placeholder), and that ``fail_placeholder`` terminalises it on a permanent
    rejection — all best-effort (never raising).
Edit boundaries: Service-layer placeholder helpers only; the drain wiring is
    covered by ``test_servicebus_tasks.py``.
Key entry points: the ``test_*`` functions.
Risky contracts: placeholder job_id IS the correlation id; supersede/fail only
    touch rows tagged ``payload.placeholder=True`` so a real job is never
    clobbered.
Validation: ``uv run pytest -q api/tests/test_servicebus_placeholder.py``.
"""

from __future__ import annotations

from typing import Any

import pytest
from api.services.blast import servicebus_placeholder as ph


class _InMemoryRepo:
    """Minimal in-memory JobState repo: create / get / update + history.

    Mirrors the contract the placeholder helpers rely on (``ResourceExistsError``
    swallowed on duplicate create, ``status ne 'deleted'`` not enforced here
    since the helpers re-read via ``get``). The jobstate table backend needs
    Azure Table Storage, so unit tests inject this instead.
    """

    def __init__(self) -> None:
        self.rows: dict[str, Any] = {}
        self.history: dict[str, list[tuple[str, dict[str, Any]]]] = {}

    def create(self, state: Any) -> Any:
        if state.job_id in self.rows:
            return self.rows[state.job_id]
        if not state.created_at:
            import datetime

            state.created_at = datetime.datetime.now(datetime.UTC).isoformat()
        state.updated_at = state.created_at
        self.rows[state.job_id] = state
        return state

    def get(self, job_id: str) -> Any:
        return self.rows.get(job_id)

    def update(self, job_id: str, **kwargs: Any) -> None:
        row = self.rows.get(job_id)
        if row is None:
            raise KeyError(job_id)
        for key, value in kwargs.items():
            setattr(row, key, value)

    def append_history(
        self, job_id: str, event: str, payload: dict[str, Any] | None = None
    ) -> None:
        self.history.setdefault(job_id, []).append((event, payload or {}))

    def get_history(self, job_id: str, limit: int = 200) -> list[dict[str, Any]]:
        import json

        out: list[dict[str, Any]] = []
        for event, payload in self.history.get(job_id, [])[:limit]:
            out.append({"event": event, "payload_json": json.dumps(payload)})
        return out


@pytest.fixture(autouse=True)
def _repo(monkeypatch: pytest.MonkeyPatch) -> _InMemoryRepo:
    repo = _InMemoryRepo()
    monkeypatch.setattr("api.services.state_repo.get_state_repo", lambda: repo)
    return repo


def _get(job_id: str):
    from api.services.state_repo import get_state_repo

    return get_state_repo().get(job_id)


def test_create_placeholder_writes_queued_row() -> None:
    ok = ph.create_queued_placeholder(
        correlation_id="corr-1",
        program="blastn",
        db="core_nt",
        owner_oid="oid-1",
        cluster_name="elb-cluster-01",
    )
    assert ok is True
    row = _get("corr-1")
    assert row is not None
    assert row.status == "queued"
    assert row.phase == "queued"
    assert row.program == "blastn"
    assert row.db == "core_nt"
    assert row.cluster_name == "elb-cluster-01"
    assert row.payload.get("placeholder") is True


def test_create_placeholder_records_enqueued_stage() -> None:
    ph.create_queued_placeholder(correlation_id="corr-2", program="blastn", db="core_nt")
    from api.services.blast.message_trace import derive_trace
    from api.services.state_repo import get_state_repo

    repo = get_state_repo()
    rows = repo.get_history("corr-2", limit=50)
    trace = derive_trace(rows)
    stages = {s["stage"] for s in trace.get("stages", [])}
    assert "enqueued" in stages


def test_create_placeholder_is_idempotent() -> None:
    assert ph.create_queued_placeholder(correlation_id="corr-3", program="blastn", db="d") is True
    # A duplicate send (at-least-once / double-click) must not raise or create a
    # second row.
    assert ph.create_queued_placeholder(correlation_id="corr-3", program="blastn", db="d") is True
    row = _get("corr-3")
    assert row is not None
    assert row.status == "queued"


def test_create_placeholder_blank_correlation_id_is_noop() -> None:
    assert ph.create_queued_placeholder(correlation_id="", program="blastn", db="d") is False


def test_supersede_soft_deletes_placeholder() -> None:
    ph.create_queued_placeholder(correlation_id="corr-4", program="blastn", db="core_nt")
    ph.supersede_placeholder("corr-4")
    row = _get("corr-4")
    assert row is not None
    assert row.status == "deleted"


def test_supersede_only_touches_placeholder_rows() -> None:
    # A non-placeholder row (e.g. a real job whose id coincidentally equals the
    # correlation id) must NOT be soft-deleted.
    from api.services.state_repo import JobState, get_state_repo

    repo = get_state_repo()
    repo.create(
        JobState(
            job_id="corr-5",
            type="blast",
            status="running",
            phase="running",
            payload={"not_a_placeholder": True},
        )
    )
    ph.supersede_placeholder("corr-5")
    row = _get("corr-5")
    assert row is not None
    assert row.status == "running"  # untouched


def test_supersede_missing_row_is_noop() -> None:
    # No exception when the placeholder does not exist (already superseded).
    ph.supersede_placeholder("corr-absent")


def test_fail_placeholder_terminalises() -> None:
    ph.create_queued_placeholder(correlation_id="corr-6", program="blastn", db="core_nt")
    ph.fail_placeholder("corr-6", error_code="servicebus_submit_rejected_400")
    row = _get("corr-6")
    assert row is not None
    assert row.status == "failed"
    assert row.phase == "failed"
    assert row.error_code == "servicebus_submit_rejected_400"


def test_fail_only_touches_placeholder_rows() -> None:
    from api.services.state_repo import JobState, get_state_repo

    repo = get_state_repo()
    repo.create(
        JobState(
            job_id="corr-7",
            type="blast",
            status="running",
            phase="running",
            payload={},
        )
    )
    ph.fail_placeholder("corr-7", error_code="x")
    row = _get("corr-7")
    assert row is not None
    assert row.status == "running"  # untouched
