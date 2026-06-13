"""Tests for the warmup task's prepare-db readiness gate.

Responsibility: Ensure `warmup_database` refuses to run when the selected DB
    is mid-copy / mid-update — auto-shard + vmtouch against incomplete volumes
    surface confusing pod-level failures minutes later, so the task must fail
    fast at the start with a clear error.
Edit boundaries: Patch `list_databases` in-process; never reach Azure.
Key entry points: `test_warmup_database_*`.
Risky contracts: `copy_status.phase == "completed"` is the only ready phase.
Validation: `uv run pytest -q api/tests/test_warmup_database_readiness.py`.
"""

from __future__ import annotations

from typing import Any

import pytest
from api.tasks.storage import warmup_database


def _stub_basics(
    monkeypatch: pytest.MonkeyPatch,
    *,
    db_match: dict[str, Any],
) -> list[dict[str, Any]]:
    state_updates: list[dict[str, Any]] = []
    monkeypatch.setattr("api.tasks.storage.get_credential", lambda: object())
    monkeypatch.setattr(
        "api.services.storage.data.list_databases",
        lambda credential, storage_account: [db_match],
    )
    monkeypatch.setattr(
        "api.tasks.storage._update_state",
        lambda job_id, phase, status="running", **extra: state_updates.append(
            {"job_id": job_id, "phase": phase, "status": status, **extra}
        ),
    )
    monkeypatch.setattr(
        "api.tasks.storage._record_task_progress",
        lambda task, phase, **meta: None,
    )
    return state_updates


def test_warmup_database_fails_when_copy_status_is_copying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_updates = _stub_basics(
        monkeypatch,
        db_match={
            "name": "core_nt",
            "file_count": 30,
            "copy_status": {"phase": "copying", "success": 30, "total_files": 800},
        },
    )

    result = warmup_database.run(
        job_id="warmup-1",
        subscription_id="sub-1",
        resource_group="rg-elb",
        storage_account="elbstg01",
        storage_resource_group="rg-elb",
        database_name="core_nt",
        cluster_name="elb-cluster",
        machine_type="Standard_E16s_v5",
        num_nodes=10,
    )

    assert result["status"] == "failed"
    assert "phase=copying" in result["error"]
    assert "30/800" in result["error"]
    assert any(
        update["phase"] == "failed" and update["status"] == "failed"
        for update in state_updates
    )


def test_warmup_database_fails_when_copy_status_is_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_updates = _stub_basics(
        monkeypatch,
        db_match={
            "name": "core_nt",
            "file_count": 750,
            "copy_status": {"phase": "partial", "success": 750, "total_files": 800, "failed": 50},
        },
    )

    result = warmup_database.run(
        job_id="warmup-1",
        subscription_id="sub-1",
        resource_group="rg-elb",
        storage_account="elbstg01",
        storage_resource_group="rg-elb",
        database_name="core_nt",
        cluster_name="elb-cluster",
        machine_type="Standard_E16s_v5",
        num_nodes=10,
    )

    assert result["status"] == "failed"
    assert "phase=partial" in result["error"]
    assert any(
        update["phase"] == "failed" and update["status"] == "failed"
        for update in state_updates
    )


def test_warmup_database_fails_when_update_in_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_updates = _stub_basics(
        monkeypatch,
        db_match={
            "name": "core_nt",
            "file_count": 800,
            "update_in_progress": True,
            "updating_to_source_version": "BLAST_DB-2026-05-20",
        },
    )

    result = warmup_database.run(
        job_id="warmup-1",
        subscription_id="sub-1",
        resource_group="rg-elb",
        storage_account="elbstg01",
        storage_resource_group="rg-elb",
        database_name="core_nt",
        cluster_name="elb-cluster",
        machine_type="Standard_E16s_v5",
        num_nodes=10,
    )

    assert result["status"] == "failed"
    assert "updating" in result["error"]
    assert "BLAST_DB-2026-05-20" in result["error"]
    assert any(
        update["phase"] == "failed" and update["status"] == "failed"
        for update in state_updates
    )


def test_warmup_database_allows_db_outside_hardcoded_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A prepared NCBI DB not in any static catalog must reach the storage gate.

    Regression for the warmup task rejecting valid prepared databases (e.g.
    ``18S_fungal_sequences``, ``ITS_RefSeq_Fungi``) with a misleading
    "unknown database" error because they were missing from a hardcoded
    ``BLAST_DATABASES`` dict. The authoritative check is the workload Storage
    catalog, so a DB that is present-but-mid-copy must fail with the
    copy-status error — proving it passed the (now removed) catalog gate.
    """
    _stub_basics(
        monkeypatch,
        db_match={
            "name": "18S_fungal_sequences",
            "file_count": 5,
            "copy_status": {"phase": "copying", "success": 5, "total_files": 12},
        },
    )

    result = warmup_database.run(
        job_id="warmup-1",
        subscription_id="sub-1",
        resource_group="rg-elb",
        storage_account="elbstg01",
        storage_resource_group="rg-elb",
        database_name="18S_fungal_sequences",
        cluster_name="elb-cluster",
        machine_type="Standard_E16s_v5",
        num_nodes=5,
    )

    assert result["status"] == "failed"
    assert "unknown database" not in result["error"]
    assert "phase=copying" in result["error"]


def test_warmup_database_unprepared_db_reports_storage_not_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DB absent from the Storage catalog reports the storage error.

    It must NOT short-circuit on a hardcoded-catalog miss — the message must
    be the authoritative "not prepared in workload storage", regardless of
    whether the name happens to be in any static list.
    """
    state_updates: list[dict[str, Any]] = []
    monkeypatch.setattr("api.tasks.storage.get_credential", lambda: object())
    monkeypatch.setattr(
        "api.services.storage.data.list_databases",
        lambda credential, storage_account: [],
    )
    monkeypatch.setattr(
        "api.tasks.storage._update_state",
        lambda job_id, phase, status="running", **extra: state_updates.append(
            {"job_id": job_id, "phase": phase, "status": status, **extra}
        ),
    )
    monkeypatch.setattr(
        "api.tasks.storage._record_task_progress",
        lambda task, phase, **meta: None,
    )

    result = warmup_database.run(
        job_id="warmup-1",
        subscription_id="sub-1",
        resource_group="rg-elb",
        storage_account="elbstg01",
        storage_resource_group="rg-elb",
        database_name="ITS_RefSeq_Fungi",
        cluster_name="elb-cluster",
        machine_type="Standard_E16s_v5",
        num_nodes=5,
    )

    assert result["status"] == "failed"
    assert "unknown database" not in result["error"]
    assert "not prepared in workload storage" in result["error"]
