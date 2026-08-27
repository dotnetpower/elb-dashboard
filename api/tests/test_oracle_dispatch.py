"""Tests for DB order-oracle claim and broker dispatch.

Responsibility: Verify ready no-op, successful JobState-before-send ordering,
    active-run adoption, and terminal rollback on broker failure.
Edit boundaries: Mock readiness/state/repository/broker dependencies; task
    execution is covered separately.
Key entry points: `test_dispatch_creates_state_before_broker_send`,
    `test_dispatch_ready_identity_is_noop`,
    `test_dispatch_broker_failure_terminalizes_new_run`.
Risky contracts: Broker send must never precede durable JobState creation and a
    failed send must release only the new active claim.
Validation: `uv run pytest -q api/tests/test_oracle_dispatch.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from api.services.db.oracle_build import OracleBuildContext
from api.services.db.oracle_dispatch import (
    _recover_terminal_active_claim,
    start_oracle_build,
)
from api.services.db.oracle_state import OracleClaimResult


def _context() -> OracleBuildContext:
    return OracleBuildContext(
        db_name="core_nt",
        source_version="v1",
        layout_schema=1,
        layout_fingerprint="layout-1",
        identity="oracle-v1:layout-1",
        shards=("00", "01"),
        shard_nodes=(("00", "node-a"), ("01", "node-b")),
    )


def _kwargs() -> dict[str, Any]:
    return {
        "subscription_id": "sub-1",
        "storage_resource_group": "rg-storage",
        "storage_account": "stelbtest",
        "cluster_resource_group": "rg-aks",
        "cluster_name": "aks-1",
        "db_name": "core_nt",
        "image": "acr.azurecr.io/ncbi/elb:1",
        "requested_source_version": "v1",
        "owner_oid": "oid-1",
        "tenant_id": "tenant-1",
    }


def _patch_base(monkeypatch: pytest.MonkeyPatch) -> tuple[object, list[str]]:
    container = object()
    order: list[str] = []
    monkeypatch.setattr(
        "api.services.db.oracle_dispatch.resolve_oracle_build_context",
        lambda *_args, **_kwargs: _context(),
    )
    monkeypatch.setattr(
        "api.services.db.oracle_dispatch.oracle_container",
        lambda *_args, **_kwargs: container,
    )
    monkeypatch.setattr(
        "api.services.db.oracle_dispatch.read_oracle_active",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "api.services.db.oracle_dispatch.read_oracle_current",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "api.services.db.oracle_dispatch.release_oracle_active",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        "api.services.db.oracle_dispatch._create_job_state",
        lambda **_kwargs: order.append("state"),
    )
    monkeypatch.setattr(
        "api.services.db.oracle_dispatch._attach_task_id",
        lambda *_args, **_kwargs: order.append("attach"),
    )
    monkeypatch.setattr(
        "api.services.db.oracle_dispatch.update_oracle_run",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        "api.services.db.oracle_dispatch.update_oracle_active",
        lambda *_args, **_kwargs: {},
    )
    return container, order


def test_dispatch_creates_state_before_broker_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _container, order = _patch_base(monkeypatch)
    monkeypatch.setattr(
        "api.services.db.oracle_dispatch.claim_oracle_build",
        lambda _container, *, db_name, document: OracleClaimResult("created", document),
    )

    def _send(*_args: Any, **_kwargs: Any) -> Any:
        order.append("send")
        assert _kwargs["task_id"]
        return type("Task", (), {"id": _kwargs["task_id"]})()

    result = start_oracle_build(object(), send_task=_send, **_kwargs())

    assert result.accepted is True
    assert result.task_id
    assert order == ["state", "attach", "send"]


def test_dispatch_ready_identity_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    container, order = _patch_base(monkeypatch)
    successes = []
    monkeypatch.setattr(
        "api.services.db.oracle_retry.record_automation_success",
        lambda value, **kwargs: successes.append((value, kwargs)) or kwargs,
    )
    monkeypatch.setattr(
        "api.services.db.oracle_dispatch.claim_oracle_build",
        lambda _container, *, db_name, document: OracleClaimResult(
            "ready",
            {
                **document,
                "status": "ready",
                "run_id": "ready-run",
                "expected_parts": 2,
            },
        ),
    )

    result = start_oracle_build(
        object(),
        send_task=lambda *_args, **_kwargs: pytest.fail("broker must not be called"),
        **_kwargs(),
    )

    assert result.accepted is False
    assert result.status == "ready"
    assert result.run_id == "ready-run"
    assert order == []
    assert successes == [
        (
            container,
            {
                "db_name": "core_nt",
                "run_id": "ready-run",
                "require_current_run": False,
            },
        )
    ]


def test_dispatch_broker_failure_terminalizes_new_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _container, order = _patch_base(monkeypatch)
    failures: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "api.services.db.oracle_dispatch.claim_oracle_build",
        lambda _container, *, db_name, document: OracleClaimResult("created", document),
    )
    monkeypatch.setattr(
        "api.services.db.oracle_dispatch._mark_enqueue_failed",
        lambda **kwargs: failures.append(kwargs),
    )

    def _fail(*_args: Any, **_kwargs: Any) -> Any:
        order.append("send")
        raise RuntimeError("broker down")

    with pytest.raises(RuntimeError, match="broker down"):
        start_oracle_build(object(), send_task=_fail, **_kwargs())

    assert order == ["state", "attach", "send"]
    assert len(failures) == 1
    assert failures[0]["document"]["db_name"] == "core_nt"


def test_enqueue_rollback_failure_keeps_jobstate_active_for_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api.services.db.oracle_dispatch import _mark_enqueue_failed

    document = {
        "job_id": "oracle-job-1",
        "db_name": "core_nt",
        "run_id": "run-1",
        "owner_operation_id": "owner-1",
        "automatic": False,
    }
    monkeypatch.setattr(
        "api.services.db.oracle_dispatch.fail_oracle_run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("blob unavailable")),
    )
    updates = []
    monkeypatch.setattr(
        "api.services.state_repo.get_state_repo",
        lambda: type(
            "Repo",
            (),
            {"update": lambda _self, *args, **kwargs: updates.append((args, kwargs))},
        )(),
    )

    _mark_enqueue_failed(
        container=object(),
        document=document,
        message="broker send failed",
    )

    assert updates == [
        (
            ("oracle-job-1",),
            {
                "status": "running",
                "phase": "enqueue_terminal_pending",
                "error_code": "oracle_enqueue_failed",
            },
        )
    ]


def test_automatic_dispatch_records_queued_state_and_task_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container, _order = _patch_base(monkeypatch)
    monkeypatch.setattr(
        "api.services.db.oracle_dispatch.claim_oracle_build",
        lambda _container, *, db_name, document: OracleClaimResult("created", document),
    )
    automation = []
    monkeypatch.setattr(
        "api.services.db.oracle_retry.record_automation_dispatch",
        lambda value, **kwargs: automation.append((value, kwargs)) or kwargs,
    )
    sent = []

    def _send(*_args: Any, **kwargs: Any) -> Any:
        sent.append(kwargs)
        return type("Task", (), {"id": kwargs["task_id"]})()

    result = start_oracle_build(object(), send_task=_send, automatic=True, **_kwargs())

    assert result.accepted is True
    assert automation[0][0] is container
    assert automation[0][1]["run_id"] == result.run_id
    assert sent[0]["kwargs"]["automatic"] is True
    assert sent[0]["kwargs"]["dispatch_token"]


def test_dispatch_redelivers_old_unclaimed_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _container, order = _patch_base(monkeypatch)
    now = datetime(2026, 8, 27, 10, 0, tzinfo=UTC)
    monkeypatch.setattr("api.services.db.oracle_dispatch._now", lambda: now)
    updates: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "api.services.db.oracle_dispatch.update_oracle_active",
        lambda *_args, **kwargs: updates.append(kwargs["updates"]) or {},
    )
    monkeypatch.setattr(
        "api.services.db.oracle_dispatch.claim_oracle_build",
        lambda _container, *, db_name, document: OracleClaimResult(
            "adopted",
            {
                **document,
                "task_id": "lost-task",
                "dispatch_token": "lost-token",
                "last_dispatched_at": "2026-08-27T09:55:00+00:00",
                "dispatch_attempt": 1,
                "execution_instance_id": "",
            },
        ),
    )
    sent: list[dict[str, Any]] = []

    def _send(*_args: Any, **kwargs: Any) -> Any:
        sent.append(kwargs)
        return type("Task", (), {"id": kwargs["task_id"]})()

    result = start_oracle_build(object(), send_task=_send, **_kwargs())

    assert result.accepted is True
    assert result.adopted is True
    assert result.task_id != "lost-task"
    assert sent[0]["kwargs"]["dispatch_token"] != "lost-token"
    assert updates[-1]["dispatch_attempt"] == 2
    assert order == ["state", "attach"]


def test_dispatch_does_not_redeliver_claimed_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _container, order = _patch_base(monkeypatch)
    monkeypatch.setattr(
        "api.services.db.oracle_dispatch.claim_oracle_build",
        lambda _container, *, db_name, document: OracleClaimResult(
            "adopted",
            {
                **document,
                "task_id": "active-task",
                "dispatch_token": "active-token",
                "last_dispatched_at": "2020-01-01T00:00:00+00:00",
                "execution_instance_id": "execution-1",
            },
        ),
    )

    result = start_oracle_build(
        object(),
        send_task=lambda *_args, **_kwargs: pytest.fail("must not redeliver"),
        **_kwargs(),
    )

    assert result.accepted is False
    assert result.task_id == "active-task"
    assert order == []


def test_recovery_releases_published_active_without_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = {"run_id": "run-1", "owner_operation_id": "owner-1"}
    monkeypatch.setattr(
        "api.services.db.oracle_dispatch.read_oracle_active",
        lambda *_args: active,
    )
    monkeypatch.setattr(
        "api.services.db.oracle_dispatch.read_oracle_current",
        lambda *_args: {"status": "ready", "run_id": "run-1"},
    )
    released = []
    monkeypatch.setattr(
        "api.services.db.oracle_dispatch.release_oracle_active",
        lambda *_args, **kwargs: released.append(kwargs) or True,
    )
    repaired = []
    monkeypatch.setattr(
        "api.services.db.oracle_dispatch.update_oracle_run",
        lambda *_args, **kwargs: repaired.append(kwargs) or kwargs["updates"],
    )
    monkeypatch.setattr(
        "api.services.db.oracle_dispatch.fail_oracle_run",
        lambda *_args, **_kwargs: pytest.fail("published run must not fail"),
    )

    result = _recover_terminal_active_claim(
        object(),
        object(),
        db_name="core_nt",
        now=datetime(2026, 8, 27, tzinfo=UTC),
    )

    assert result == "published"
    assert released[0]["owner_operation_id"] == "owner-1"
    assert repaired[0]["run_id"] == "run-1"
    assert repaired[0]["updates"]["status"] == "ready"


def test_recovery_terminalizes_expired_automatic_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = {
        "run_id": "run-1",
        "job_id": "job-1",
        "owner_operation_id": "owner-1",
        "deadline_at": "2026-08-27T09:00:00+00:00",
        "automatic": True,
        "subscription_id": "sub-1",
        "cluster_resource_group": "rg-aks",
        "cluster_name": "aks-1",
        "namespace": "default",
        "job_names": ["oracle-00", "oracle-01"],
    }
    monkeypatch.setattr(
        "api.services.db.oracle_dispatch.read_oracle_active",
        lambda *_args: active,
    )
    monkeypatch.setattr(
        "api.services.db.oracle_dispatch.read_oracle_current",
        lambda *_args: None,
    )
    failed = []
    monkeypatch.setattr(
        "api.services.db.oracle_dispatch.fail_oracle_run",
        lambda *_args, **kwargs: failed.append(kwargs) or kwargs,
    )
    state_updates = []
    monkeypatch.setattr(
        "api.services.state_repo.get_state_repo",
        lambda: type(
            "Repo",
            (),
            {"update": lambda _self, *args, **kwargs: state_updates.append((args, kwargs))},
        )(),
    )
    retry_failures = []
    monkeypatch.setattr(
        "api.services.db.oracle_retry.record_automation_failure",
        lambda *_args, **kwargs: retry_failures.append(kwargs) or kwargs,
    )
    cleanup_calls = []
    monkeypatch.setattr(
        "api.services.db.oracle_runtime.cleanup_oracle_jobs",
        lambda *_args, **kwargs: (
            cleanup_calls.append(kwargs) or {"deleted": kwargs["job_names"], "errors": []}
        ),
    )

    result = _recover_terminal_active_claim(
        object(),
        object(),
        db_name="core_nt",
        now=datetime(2026, 8, 27, 10, 0, tzinfo=UTC),
    )

    assert result == "expired"
    assert failed[0]["error_code"] == "oracle_execution_deadline_exceeded"
    assert state_updates[0][0] == ("job-1",)
    assert retry_failures[0]["run_id"] == "run-1"
    assert cleanup_calls[0]["job_names"] == ["oracle-00", "oracle-01"]


def test_recovery_retains_published_anchor_until_history_repair_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = {"run_id": "run-1", "owner_operation_id": "owner-1"}
    monkeypatch.setattr(
        "api.services.db.oracle_dispatch.read_oracle_active",
        lambda *_args: active,
    )
    monkeypatch.setattr(
        "api.services.db.oracle_dispatch.read_oracle_current",
        lambda *_args: {"status": "ready", "run_id": "run-1"},
    )
    monkeypatch.setattr(
        "api.services.db.oracle_dispatch.update_oracle_run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("history unavailable")),
    )
    monkeypatch.setattr(
        "api.services.db.oracle_dispatch.release_oracle_active",
        lambda *_args, **_kwargs: pytest.fail("anchor must remain"),
    )

    result = _recover_terminal_active_claim(
        object(),
        object(),
        db_name="core_nt",
        now=datetime(2026, 8, 27, tzinfo=UTC),
    )

    assert result == "published_pending"
