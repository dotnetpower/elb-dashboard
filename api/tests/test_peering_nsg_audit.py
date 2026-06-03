"""Unit tests for the peering-NSG audit session lifecycle.

Responsibility: Verify `audit_session` guarantees a terminal/interrupted event and that audit
backend failures never propagate out of the helpers.
Edit boundaries: Test-only; exercises `api.services.peering_nsg_audit` in isolation with the
`ops_audit` backend monkeypatched.
Key entry points: `test_session_records_terminal_event`, `test_session_emits_interrupted_on_raise`,
`test_start_failure_is_swallowed`, `test_event_without_job_id_returns_false`
Risky contracts: A raise inside the session body must still emit `interrupted`; a backend that
raises must be swallowed (logged), never re-raised.
Validation: `uv run pytest -q api/tests/test_peering_nsg_audit.py`.
"""

from __future__ import annotations

from typing import Any

import api.services.peering_nsg_audit as audit_mod
import pytest
from api.auth import CallerIdentity


def _caller() -> CallerIdentity:
    return CallerIdentity(
        object_id="oid-1",
        tenant_id="t",
        upn="user@example.com",
        raw_token="",
        claims={},
    )


def test_event_without_job_id_returns_false() -> None:
    assert audit_mod.record_audit_event(None, "completed", {}) is False


def test_session_records_terminal_event(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[str] = []
    events: list[tuple[str, str, dict[str, Any]]] = []

    def fake_record_db_op(**kwargs: Any) -> str:
        started.append(kwargs["op"])
        return "job-123"

    def fake_record_db_op_event(job_id: str, event: str, payload: dict[str, Any]) -> None:
        events.append((job_id, event, payload))

    import api.services.db.ops_audit as ops_audit

    monkeypatch.setattr(ops_audit, "record_db_op", fake_record_db_op)
    monkeypatch.setattr(ops_audit, "record_db_op_event", fake_record_db_op_event)

    with audit_mod.audit_session(
        op="apply-nsg-rule",
        caller=_caller(),
        target_nsg_name="nsg-x",
        destination_ip="10.0.0.4",
        extra={"k": "v"},
    ) as (job_id, set_terminal):
        assert job_id == "job-123"
        assert set_terminal("completed", {"applied": True}) is True

    assert started == ["apply-nsg-rule"]
    # Only the explicit terminal event; no phantom interrupted row.
    assert events == [("job-123", "completed", {"applied": True})]


def test_session_emits_interrupted_on_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[tuple[str, str, dict[str, Any]]] = []

    import api.services.db.ops_audit as ops_audit

    monkeypatch.setattr(ops_audit, "record_db_op", lambda **kw: "job-err")
    monkeypatch.setattr(
        ops_audit,
        "record_db_op_event",
        lambda job_id, event, payload: events.append((job_id, event, payload)),
    )

    with pytest.raises(RuntimeError):
        with audit_mod.audit_session(
            op="apply-nsg-rule",
            caller=_caller(),
            target_nsg_name="nsg-y",
            destination_ip="10.0.0.5",
            extra={},
        ):
            raise RuntimeError("boom")

    assert events == [("job-err", "interrupted", {"reason": "no_terminal_event_recorded"})]


def test_start_failure_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    import api.services.db.ops_audit as ops_audit

    def boom(**kwargs: Any) -> str:
        raise RuntimeError("audit backend down")

    monkeypatch.setattr(ops_audit, "record_db_op", boom)

    # No job id -> no terminal write attempted, and no exception escapes.
    with audit_mod.audit_session(
        op="apply-nsg-rule",
        caller=_caller(),
        target_nsg_name="nsg-z",
        destination_ip="10.0.0.6",
        extra={},
    ) as (job_id, set_terminal):
        assert job_id is None
        assert set_terminal("completed", {}) is False
