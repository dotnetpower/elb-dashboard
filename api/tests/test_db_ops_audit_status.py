"""Tests for `api.services.db.ops_audit.record_db_op` status handling.

Responsibility: Prove the audit-row status contract — asynchronous ops are
    born ``queued`` (default), synchronous ops born terminal (``completed``)
    so they never leak active forever, and the appended history event name
    mirrors the status.
Edit boundaries: Pure unit tests with a fake state repo. No Table / Azure IO.
Key entry points: see per-test docstrings.
Risky contracts: A synchronous cancel/delete audit row MUST be terminal at
    creation; the stale-dbops reconciler only mops up legacy rows.
Validation: `uv run pytest -q api/tests/test_db_ops_audit_status.py`.
"""

from __future__ import annotations

from typing import Any

import pytest
from api.auth import CallerIdentity
from api.services.db import ops_audit


class _FakeRepo:
    def __init__(self) -> None:
        self.created: list[Any] = []
        self.history: list[tuple[str, str]] = []

    def create(self, state: Any) -> Any:
        self.created.append(state)
        return state

    def append_history(self, job_id: str, event: str, payload: dict[str, Any]) -> None:
        self.history.append((job_id, event))


def _caller() -> CallerIdentity:
    return CallerIdentity(
        object_id="oid-1",
        tenant_id="tid-1",
        upn=None,
        raw_token="",
        claims={},
    )


@pytest.fixture
def repo(monkeypatch: pytest.MonkeyPatch) -> _FakeRepo:
    fake = _FakeRepo()
    monkeypatch.setattr(ops_audit, "get_state_repo", lambda: fake)
    return fake


def test_async_op_defaults_to_queued(repo: _FakeRepo) -> None:
    ops_audit.record_db_op(
        op="prepare_db_aks", caller=_caller(), account_name="acct", db_name="nt"
    )
    state = repo.created[0]
    assert state.status == "queued"
    assert state.phase == "queued"
    assert repo.history[0][1] == "started"


def test_sync_op_records_completed_terminal(repo: _FakeRepo) -> None:
    ops_audit.record_db_op(
        op="prepare_db_cancel",
        caller=_caller(),
        account_name="acct",
        db_name="nt",
        status="completed",
    )
    state = repo.created[0]
    assert state.status == "completed"
    assert state.phase == "completed"  # phase defaults to status
    # The history event mirrors the terminal status so the audit timeline is
    # not stuck on a perpetual "started".
    assert repo.history[0][1] == "completed"


def test_explicit_phase_override(repo: _FakeRepo) -> None:
    ops_audit.record_db_op(
        op="prepare_db_delete",
        caller=_caller(),
        account_name="acct",
        db_name="nt",
        status="completed",
        phase="deleted",
    )
    state = repo.created[0]
    assert state.status == "completed"
    assert state.phase == "deleted"
    assert repo.history[0][1] == "completed"
