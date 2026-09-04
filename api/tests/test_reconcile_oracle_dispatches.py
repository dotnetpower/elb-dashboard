"""Tests for durable order-oracle dispatch recovery.

Responsibility: Verify active oracle rows replay through shared dispatch while
    malformed and expected-blocked rows fail closed without broker side effects.
Edit boundaries: JobState/dispatch/Celery dependencies are mocked; state-machine
    behavior remains covered in oracle dispatch/task tests.
Key entry points: `test_reconciler_replays_valid_active_oracle`,
    `test_reconciler_skips_malformed_payload`.
Risky contracts: Only active `type=oracle` rows are scanned and the persisted
    resource coordinates are forwarded without environment fallback.
Validation: `uv run pytest -q api/tests/test_reconcile_oracle_dispatches.py`.
"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from api.tasks.storage.reconcile_oracle_dispatches import (
    _reset_completed_orphan,
    reconcile_oracle_dispatches,
)

_RECONCILE_MODULE = importlib.import_module(
    "api.tasks.storage.reconcile_oracle_dispatches"
)


def _payload() -> dict[str, object]:
    return {
        "run_id": "run-1",
        "subscription_id": "sub-1",
        "storage_resource_group": "rg-storage",
        "storage_account": "stelbtest",
        "cluster_resource_group": "rg-aks",
        "cluster_name": "aks-1",
        "db_name": "core_nt",
        "image": "acr.azurecr.io/ncbi/elb:1",
        "requested_source_version": "v1",
        "requested_by": "owner-1",
        "automatic": True,
    }


def test_reconciler_replays_valid_active_oracle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = SimpleNamespace(
        list_active=lambda **kwargs: [
            SimpleNamespace(job_id="oracle-job-1", tenant_id="tenant-1", payload=_payload())
        ]
    )
    monkeypatch.setattr("api.services.state_repo.get_state_repo", lambda: repo)
    monkeypatch.setattr("api.services.get_credential", lambda: object())
    monkeypatch.setenv("AUTO_ORACLE_RECONCILE_ENABLED", "true")
    monkeypatch.setenv("ENFORCE_AUTO_ORACLE_RBAC", "true")
    monkeypatch.setattr(
        "api.services.db.oracle_state.oracle_container",
        lambda *_args: object(),
    )
    monkeypatch.setattr(
        "api.services.db.oracle_dispatch._recover_terminal_active_claim",
        lambda *_args, **_kwargs: "active",
    )
    calls = []
    monkeypatch.setattr(
        "api.services.db.oracle_dispatch.start_oracle_build",
        lambda *_args, **kwargs: (
            calls.append(kwargs)
            or SimpleNamespace(
                accepted=True,
                run_id="run-1",
                status="queued",
            )
        ),
    )

    result = reconcile_oracle_dispatches.run()

    assert result["accepted"] == [{"job_id": "oracle-job-1", "run_id": "run-1", "status": "queued"}]
    assert calls[0]["cluster_resource_group"] == "rg-aks"
    assert calls[0]["storage_resource_group"] == "rg-storage"
    assert calls[0]["automatic"] is True


def test_completed_worker_loss_orphan_resets_only_after_full_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _payload()
    active = {
        **payload,
        "task_id": "lost-task",
        "owner_operation_id": "owner-run-1",
        "execution_instance_id": "execution-1",
        "updated_at": "2026-09-04T10:15:00+00:00",
        "job_names": ["oracle-00"],
        "expected_shards": ["00"],
        "part_prefix": "metadata/oracles/core_nt/parts/run-1/",
        "namespace": "default",
    }
    monkeypatch.setattr(
        "api.services.db.oracle_state.read_oracle_active",
        lambda *_args: active,
    )
    monkeypatch.setattr(_RECONCILE_MODULE, "_celery_task_ids", lambda _app: set())
    monkeypatch.setattr(
        "api.services.k8s.monitoring.k8s_get_jobs",
        lambda *_args: [{"name": "oracle-00", "status": "Complete"}],
    )
    monkeypatch.setattr(
        "api.services.db.oracle_runtime.validate_oracle_parts",
        lambda *_args, **_kwargs: {"ready": True},
    )
    resets: list[dict[str, object]] = []
    monkeypatch.setattr(
        "api.services.db.oracle_state.reset_oracle_execution_for_redelivery",
        lambda *_args, **kwargs: resets.append(kwargs) or True,
    )

    recovered = _reset_completed_orphan(
        celery_app=object(),
        credential=object(),
        container=object(),
        payload=payload,
        now=datetime(2026, 9, 4, 10, 22, tzinfo=UTC),
    )

    assert recovered is True
    assert resets[0]["expected_execution_instance_id"] == "execution-1"


def test_completed_orphan_does_not_reset_while_task_is_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _payload()
    monkeypatch.setattr(
        "api.services.db.oracle_state.read_oracle_active",
        lambda *_args: {
            **payload,
            "task_id": "live-task",
            "owner_operation_id": "owner-run-1",
            "execution_instance_id": "execution-1",
            "updated_at": "2026-09-04T10:15:00+00:00",
            "job_names": ["oracle-00"],
            "expected_shards": ["00"],
        },
    )
    monkeypatch.setattr(
        _RECONCILE_MODULE,
        "_celery_task_ids",
        lambda _app: {"live-task"},
    )
    monkeypatch.setattr(
        "api.services.k8s.monitoring.k8s_get_jobs",
        lambda *_args: pytest.fail("active tasks must not inspect Kubernetes"),
    )

    assert not _reset_completed_orphan(
        celery_app=object(),
        credential=object(),
        container=object(),
        payload=payload,
        now=datetime(2026, 9, 4, 10, 22, tzinfo=UTC),
    )


def test_reconciler_redelivers_completed_worker_loss_orphan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = SimpleNamespace(
        list_active=lambda **_kwargs: [
            SimpleNamespace(job_id="oracle-job-1", tenant_id="tenant-1", payload=_payload())
        ]
    )
    monkeypatch.setattr("api.services.state_repo.get_state_repo", lambda: repo)
    monkeypatch.setattr("api.services.get_credential", lambda: object())
    monkeypatch.setenv("AUTO_ORACLE_RECONCILE_ENABLED", "true")
    monkeypatch.setenv("ENFORCE_AUTO_ORACLE_RBAC", "true")
    monkeypatch.setattr("api.services.db.oracle_state.oracle_container", lambda *_args: object())
    monkeypatch.setattr(
        "api.services.db.oracle_dispatch._recover_terminal_active_claim",
        lambda *_args, **_kwargs: "active",
    )
    monkeypatch.setattr(_RECONCILE_MODULE, "_reset_completed_orphan", lambda **_kwargs: True)
    calls: list[bool] = []

    def dispatch(*_args, **_kwargs):
        calls.append(True)
        if len(calls) == 1:
            return SimpleNamespace(accepted=False, run_id="run-1", status="running")
        return SimpleNamespace(accepted=True, run_id="run-1", status="queued")

    monkeypatch.setattr("api.services.db.oracle_dispatch.start_oracle_build", dispatch)

    result = reconcile_oracle_dispatches.run()

    assert len(calls) == 2
    assert result["accepted"] == [
        {"job_id": "oracle-job-1", "run_id": "run-1", "status": "queued"}
    ]


def test_reconciler_processes_oldest_rows_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        SimpleNamespace(
            job_id=f"oracle-job-{index:02d}",
            tenant_id="tenant-1",
            updated_at=f"2026-08-27T00:{index:02d}:00Z",
            payload=_payload(),
        )
        for index in reversed(range(12))
    ]
    repo = SimpleNamespace(list_active=lambda **kwargs: rows)
    monkeypatch.setattr("api.services.state_repo.get_state_repo", lambda: repo)
    monkeypatch.setattr("api.services.get_credential", lambda: object())
    monkeypatch.setenv("AUTO_ORACLE_RECONCILE_ENABLED", "true")
    monkeypatch.setenv("ENFORCE_AUTO_ORACLE_RBAC", "true")
    monkeypatch.setattr("api.services.db.oracle_state.oracle_container", lambda *_args: object())
    monkeypatch.setattr(
        "api.services.db.oracle_dispatch._recover_terminal_active_claim",
        lambda *_args, **_kwargs: "active",
    )
    processed = []
    monkeypatch.setattr(
        "api.services.db.oracle_dispatch.start_oracle_build",
        lambda *_args, **_kwargs: (
            processed.append(True)
            or SimpleNamespace(accepted=False, run_id="run-1", status="running")
        ),
    )

    result = reconcile_oracle_dispatches.run()

    assert result["scanned"] == 10
    assert len(processed) == 10
    assert [item["job_id"] for item in result["skipped"]] == [
        f"oracle-job-{index:02d}" for index in range(10)
    ]


def test_reconciler_skips_malformed_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = SimpleNamespace(
        list_active=lambda **kwargs: [
            SimpleNamespace(job_id="oracle-job-bad", payload={"db_name": "core_nt"})
        ]
    )
    monkeypatch.setattr("api.services.state_repo.get_state_repo", lambda: repo)
    monkeypatch.setattr("api.services.get_credential", lambda: object())
    monkeypatch.setattr(
        "api.services.db.oracle_dispatch.start_oracle_build",
        lambda *_args, **_kwargs: pytest.fail("invalid payload must not dispatch"),
    )

    result = reconcile_oracle_dispatches.run()

    assert result["skipped"] == [{"job_id": "oracle-job-bad", "reason": "invalid_payload"}]


@pytest.mark.parametrize("recovery", ["none", "published", "expired"])
def test_reconciler_never_creates_new_run_without_active_claim(
    monkeypatch: pytest.MonkeyPatch,
    recovery: str,
) -> None:
    repo = SimpleNamespace(
        list_active=lambda **kwargs: [
            SimpleNamespace(job_id="oracle-job-1", tenant_id="tenant-1", payload=_payload())
        ]
    )
    monkeypatch.setattr("api.services.state_repo.get_state_repo", lambda: repo)
    monkeypatch.setattr("api.services.get_credential", lambda: object())
    monkeypatch.setenv("AUTO_ORACLE_RECONCILE_ENABLED", "true")
    monkeypatch.setenv("ENFORCE_AUTO_ORACLE_RBAC", "true")
    monkeypatch.setattr("api.services.db.oracle_state.oracle_container", lambda *_args: object())
    monkeypatch.setattr(
        "api.services.db.oracle_dispatch._recover_terminal_active_claim",
        lambda *_args, **_kwargs: recovery,
    )
    monkeypatch.setattr(
        "api.services.db.oracle_state.read_oracle_current",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        "api.services.db.oracle_state.read_oracle_run",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        "api.services.db.oracle_dispatch.start_oracle_build",
        lambda *_args, **_kwargs: pytest.fail("terminal recovery must not create a run"),
    )

    result = reconcile_oracle_dispatches.run()

    assert result["skipped"] == [{"job_id": "oracle-job-1", "reason": f"recovery_{recovery}"}]


def test_reconciler_does_not_reactivate_automatic_run_when_guard_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AUTO_ORACLE_RECONCILE_ENABLED", raising=False)
    monkeypatch.delenv("ENFORCE_AUTO_ORACLE_RBAC", raising=False)
    repo = SimpleNamespace(
        list_active=lambda **kwargs: [
            SimpleNamespace(job_id="oracle-job-1", tenant_id="tenant-1", payload=_payload())
        ]
    )
    monkeypatch.setattr("api.services.state_repo.get_state_repo", lambda: repo)
    monkeypatch.setattr("api.services.get_credential", lambda: object())
    monkeypatch.setattr("api.services.db.oracle_state.oracle_container", lambda *_args: object())
    recoveries = []
    monkeypatch.setattr(
        "api.services.db.oracle_dispatch._recover_terminal_active_claim",
        lambda *_args, **_kwargs: recoveries.append(True) or "active",
    )
    monkeypatch.setattr(
        "api.services.db.oracle_dispatch.start_oracle_build",
        lambda *_args, **_kwargs: pytest.fail("guard-off run must not replay"),
    )

    result = reconcile_oracle_dispatches.run()

    assert result["skipped"] == [{"job_id": "oracle-job-1", "reason": "auto_oracle_guard_off"}]
    assert recoveries == [True]


def test_reconciler_repairs_jobstate_when_current_published_and_active_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    updates = []
    repo = SimpleNamespace(
        list_active=lambda **kwargs: [
            SimpleNamespace(job_id="oracle-job-1", tenant_id="tenant-1", payload=_payload())
        ],
        update=lambda *args, **kwargs: updates.append((args, kwargs)),
    )
    monkeypatch.setattr("api.services.state_repo.get_state_repo", lambda: repo)
    monkeypatch.setattr("api.services.get_credential", lambda: object())
    monkeypatch.setattr("api.services.db.oracle_state.oracle_container", lambda *_args: object())
    monkeypatch.setattr(
        "api.services.db.oracle_dispatch._recover_terminal_active_claim",
        lambda *_args, **_kwargs: "none",
    )
    monkeypatch.setattr(
        "api.services.db.oracle_state.read_oracle_current",
        lambda *_args: {"status": "ready", "run_id": "run-1"},
    )
    monkeypatch.setattr(
        "api.services.db.oracle_dispatch.start_oracle_build",
        lambda *_args, **_kwargs: pytest.fail("published run must not dispatch"),
    )

    result = reconcile_oracle_dispatches.run()

    assert updates == [
        (
            ("oracle-job-1",),
            {"status": "completed", "phase": "completed", "error_code": ""},
        )
    ]
    assert result["skipped"] == [
        {"job_id": "oracle-job-1", "reason": "recovery_published_no_active"}
    ]


def test_reconciler_repairs_jobstate_from_terminal_run_when_active_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    updates = []
    repo = SimpleNamespace(
        list_active=lambda **kwargs: [
            SimpleNamespace(job_id="oracle-job-1", tenant_id="tenant-1", payload=_payload())
        ],
        update=lambda *args, **kwargs: updates.append((args, kwargs)),
    )
    monkeypatch.setattr("api.services.state_repo.get_state_repo", lambda: repo)
    monkeypatch.setattr("api.services.get_credential", lambda: object())
    monkeypatch.setattr("api.services.db.oracle_state.oracle_container", lambda *_args: object())
    monkeypatch.setattr(
        "api.services.db.oracle_dispatch._recover_terminal_active_claim",
        lambda *_args, **_kwargs: "none",
    )
    monkeypatch.setattr(
        "api.services.db.oracle_state.read_oracle_current",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        "api.services.db.oracle_state.read_oracle_run",
        lambda *_args: {
            "status": "failed",
            "error_code": "oracle_job_failed",
        },
    )

    result = reconcile_oracle_dispatches.run()

    assert updates == [
        (
            ("oracle-job-1",),
            {
                "status": "failed",
                "phase": "failed",
                "error_code": "oracle_job_failed",
            },
        )
    ]
    assert result["skipped"] == [
        {"job_id": "oracle-job-1", "reason": "recovery_terminal_no_active"}
    ]
