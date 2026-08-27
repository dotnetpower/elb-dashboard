"""Tests for conservative bounded order-oracle retention.

Responsibility: Verify age selection preserves current, previous-ready, active,
    referenced, recent, malformed, and nonterminal runs.
Edit boundaries: Pure planner and default-off task checks only; Azure deletion
    mechanics use the same tested Blob primitives as oracle state/reference code.
Key entry points: `test_retention_planner_preserves_protected_runs`,
    `test_retention_task_defaults_disabled`.
Risky contracts: No protected or indeterminate run may become a candidate.
Validation: `uv run pytest -q api/tests/test_oracle_retention.py`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from api.services.db.oracle_retention import (
    purge_oracle_history,
    select_oracle_runs_for_retention,
)
from api.tasks.storage.oracle_retention import purge_oracle_history_task
from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError


def test_retention_planner_preserves_protected_runs() -> None:
    documents = {
        "current": {"status": "ready", "finished_at": "2026-08-20T00:00:00Z"},
        "previous": {"status": "ready", "finished_at": "2026-08-01T00:00:00Z"},
        "old-ready": {"status": "ready", "finished_at": "2026-07-01T00:00:00Z"},
        "active": {"status": "failed", "finished_at": "2026-07-02T00:00:00Z"},
        "referenced": {"status": "failed", "finished_at": "2026-07-03T00:00:00Z"},
        "recent": {"status": "failed", "finished_at": "2026-08-20T00:00:00Z"},
        "malformed": {"status": "failed", "finished_at": "not-a-date"},
        "running": {"status": "running", "finished_at": "2026-07-01T00:00:00Z"},
        "old-failed": {"status": "failed", "finished_at": "2026-07-04T00:00:00Z"},
    }

    selected = select_oracle_runs_for_retention(
        documents,
        current_run_id="current",
        active_run_id="active",
        referenced_run_ids={"referenced"},
        now=datetime(2026, 8, 27, tzinfo=UTC),
        days=14,
    )

    assert selected == ["old-ready", "old-failed"]


def test_retention_task_defaults_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AUTO_ORACLE_RETENTION_ENABLED", raising=False)

    assert purge_oracle_history_task.run() == {"status": "disabled", "targets": []}


def test_retention_task_advances_its_preference_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api.services.auto_oracle import AutoOraclePreference

    monkeypatch.setenv("AUTO_ORACLE_RETENTION_ENABLED", "true")
    monkeypatch.setattr("api.services.get_credential", lambda: object())
    monkeypatch.setattr(
        "api.services.auto_oracle.get_auto_oracle_scan_cursor",
        lambda name: "retention-page-1" if name == "retention" else "",
    )
    preference = AutoOraclePreference(
        subscription_id="sub",
        cluster_resource_group="rg",
        cluster_name="cluster",
        storage_resource_group="storage-rg",
        storage_account="stelbtest",
        db_name="core_nt",
    )
    page_calls = []
    monkeypatch.setattr(
        "api.services.auto_oracle.list_auto_oracle_preference_page",
        lambda **kwargs: page_calls.append(kwargs) or ([preference], "retention-page-2"),
    )
    cursor_writes = []
    monkeypatch.setattr(
        "api.services.auto_oracle.save_auto_oracle_scan_cursor",
        lambda name, cursor: cursor_writes.append((name, cursor)),
    )
    monkeypatch.setattr(
        "api.services.db.oracle_state.oracle_container",
        lambda *_args: object(),
    )
    monkeypatch.setattr(
        "api.services.db.oracle_retention.purge_oracle_history",
        lambda *_args, **kwargs: {"db_name": kwargs["db_name"]},
    )

    result = purge_oracle_history_task.run()

    assert page_calls == [{"limit": 50, "continuation_token": "retention-page-1"}]
    assert cursor_writes == [("retention", "retention-page-2")]
    assert result["targets"] == [{"db_name": "core_nt"}]
    assert result["cursor_reset"] is False


def test_retention_task_resets_invalid_preference_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTO_ORACLE_RETENTION_ENABLED", "true")
    monkeypatch.setattr("api.services.get_credential", lambda: object())
    monkeypatch.setattr(
        "api.services.auto_oracle.get_auto_oracle_scan_cursor",
        lambda _name: "corrupt-token",
    )
    calls = []

    def _page(**kwargs):
        calls.append(kwargs["continuation_token"])
        if kwargs["continuation_token"]:
            raise ValueError("invalid token")
        return [], ""

    monkeypatch.setattr("api.services.auto_oracle.list_auto_oracle_preference_page", _page)
    monkeypatch.setattr(
        "api.services.auto_oracle.save_auto_oracle_scan_cursor",
        lambda *_args: None,
    )

    result = purge_oracle_history_task.run()

    assert calls == ["corrupt-token", ""]
    assert result == {
        "status": "completed",
        "targets": [],
        "cursor_reset": True,
    }


def test_retention_task_isolates_target_failure_and_advances_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api.services.auto_oracle import AutoOraclePreference

    monkeypatch.setenv("AUTO_ORACLE_RETENTION_ENABLED", "true")
    monkeypatch.setattr("api.services.get_credential", lambda: object())
    monkeypatch.setattr("api.services.auto_oracle.get_auto_oracle_scan_cursor", lambda _name: "")
    preferences = [
        AutoOraclePreference(
            subscription_id="sub",
            cluster_resource_group="rg",
            cluster_name="cluster",
            storage_resource_group="storage-rg",
            storage_account=f"stelb{index}",
            db_name=f"db{index}",
        )
        for index in range(2)
    ]
    monkeypatch.setattr(
        "api.services.auto_oracle.list_auto_oracle_preference_page",
        lambda **_kwargs: (preferences, "next-page"),
    )
    cursor_writes = []
    monkeypatch.setattr(
        "api.services.auto_oracle.save_auto_oracle_scan_cursor",
        lambda name, cursor: cursor_writes.append((name, cursor)),
    )

    def _container(_credential, storage_account):
        if storage_account == "stelb0":
            raise RuntimeError("storage unavailable")
        return object()

    monkeypatch.setattr("api.services.db.oracle_state.oracle_container", _container)
    monkeypatch.setattr(
        "api.services.db.oracle_retention.purge_oracle_history",
        lambda *_args, **kwargs: {
            "db_name": kwargs["db_name"],
            "status": "completed",
        },
    )

    result = purge_oracle_history_task.run()

    assert result["status"] == "partial"
    assert result["targets"][0]["status"] == "failed"
    assert result["targets"][1]["status"] == "completed"
    assert cursor_writes == [("retention", "next-page")]


class _Download:
    def __init__(self, value: bytes) -> None:
        self.value = value

    def readall(self) -> bytes:
        return self.value


class _Blob:
    def __init__(self, container: _Container, path: str) -> None:
        self.container = container
        self.path = path

    def download_blob(self, *, offset: int, length: int) -> _Download:
        del offset
        if self.path not in self.container.values:
            raise ResourceNotFoundError("missing")
        return _Download(self.container.values[self.path][:length])

    def upload_blob(self, data: str, *, overwrite: bool) -> None:
        if not overwrite and self.path in self.container.values:
            raise ResourceExistsError("exists")
        self.container.values[self.path] = data.encode()
        self.container.events.append(("upload", self.path))
        if "/gc/" in self.path and self.container.inject_reference_on_marker:
            reference = self.container.inject_reference_on_marker
            self.container.values[reference] = b"{}"

    def delete_blob(self) -> None:
        if self.path not in self.container.values:
            raise ResourceNotFoundError("missing")
        del self.container.values[self.path]
        self.container.events.append(("delete", self.path))


class _Container:
    def __init__(self, values: dict[str, object]) -> None:
        self.values = {
            path: (json.dumps(value).encode() if isinstance(value, dict) else str(value).encode())
            for path, value in values.items()
        }
        self.events: list[tuple[str, str]] = []
        self.inject_reference_on_marker = ""

    def get_blob_client(self, path: str) -> _Blob:
        return _Blob(self, path)

    def list_blobs(self, *, name_starts_with: str, **_kwargs):
        return [
            SimpleNamespace(name=path)
            for path in sorted(self.values)
            if path.startswith(name_starts_with)
        ]


def _run_path(run_id: str) -> str:
    return f"metadata/oracles/core_nt/runs/{run_id}/status.json"


def _part_path(run_id: str, shard: str = "00") -> str:
    return f"metadata/oracles/core_nt/parts/{run_id}/{shard}.txt"


def test_purge_marks_then_deletes_parts_before_status() -> None:
    container = _Container(
        {
            "metadata/oracles/core_nt/status.json": {
                "status": "ready",
                "run_id": "current",
            },
            _run_path("current"): {
                "status": "ready",
                "finished_at": "2026-08-20T00:00:00Z",
            },
            _run_path("previous"): {
                "status": "ready",
                "finished_at": "2026-08-01T00:00:00Z",
            },
            _run_path("old"): {
                "status": "failed",
                "finished_at": "2026-07-01T00:00:00Z",
            },
            _part_path("old"): "accession\n",
        }
    )

    result = purge_oracle_history(
        container,
        db_name="core_nt",
        dry_run=False,
        now=datetime(2026, 8, 27, tzinfo=UTC),
    )

    assert result["purged_runs"] == ["old"]
    marker = "metadata/oracles/core_nt/gc/old.json"
    assert _part_path("old") not in container.values
    assert _run_path("old") not in container.values
    assert (
        container.events.index(("upload", marker))
        < container.events.index(("delete", _part_path("old")))
        < container.events.index(("delete", _run_path("old")))
    )
    assert _run_path("current") in container.values
    assert _run_path("previous") in container.values
    # Permanent tombstone: stale resolvers cannot reference deleted parts.
    assert marker in container.values


def test_purge_preserves_reference_and_never_claims_it() -> None:
    reference = "metadata/oracles/core_nt/references/old/job.json"
    container = _Container(
        {
            _run_path("old"): {
                "status": "failed",
                "finished_at": "2026-07-01T00:00:00Z",
            },
            _part_path("old"): "accession\n",
            reference: {"run_id": "old"},
        }
    )

    result = purge_oracle_history(
        container,
        db_name="core_nt",
        dry_run=False,
        now=datetime(2026, 8, 27, tzinfo=UTC),
    )

    assert result["planned_runs"] == []
    assert _run_path("old") in container.values
    assert not any("/gc/old.json" in path for _event, path in container.events)


def test_purge_fails_closed_when_inventory_document_is_invalid() -> None:
    container = _Container(
        {
            _run_path("old"): "not-json",
            _part_path("old"): "accession\n",
        }
    )

    result = purge_oracle_history(
        container,
        db_name="core_nt",
        dry_run=False,
        now=datetime(2026, 8, 27, tzinfo=UTC),
    )

    assert result["status"] == "partial"
    assert result["errors"][0]["run_id"] == "old"
    assert not any(event == "delete" for event, _path in container.events)
    assert _part_path("old") in container.values


def test_purge_aborts_when_reference_appears_after_marker_claim() -> None:
    container = _Container(
        {
            _run_path("old"): {
                "status": "failed",
                "finished_at": "2026-07-01T00:00:00Z",
            },
            _part_path("old"): "accession\n",
        }
    )
    container.inject_reference_on_marker = "metadata/oracles/core_nt/references/old/racing-job.json"

    result = purge_oracle_history(
        container,
        db_name="core_nt",
        dry_run=False,
        now=datetime(2026, 8, 27, tzinfo=UTC),
    )

    assert result["purged_runs"] == []
    assert _part_path("old") in container.values
    assert _run_path("old") in container.values
    assert "metadata/oracles/core_nt/gc/old.json" not in container.values


def test_purge_budget_keeps_status_until_all_parts_are_deleted() -> None:
    container = _Container(
        {
            _run_path("old"): {
                "status": "failed",
                "finished_at": "2026-07-01T00:00:00Z",
            },
            _part_path("old", "00"): "a\n",
            _part_path("old", "01"): "b\n",
            _part_path("old", "02"): "c\n",
        }
    )

    result = purge_oracle_history(
        container,
        db_name="core_nt",
        dry_run=False,
        max_blobs=2,
        now=datetime(2026, 8, 27, tzinfo=UTC),
    )

    assert result["deleted_blobs"] == 2
    assert result["purged_runs"] == []
    assert _run_path("old") in container.values
    assert len([path for path in container.values if "/parts/old/" in path]) == 1
    assert "metadata/oracles/core_nt/gc/old.json" in container.values

    resumed = purge_oracle_history(
        container,
        db_name="core_nt",
        dry_run=False,
        max_blobs=2,
        now=datetime(2026, 8, 27, tzinfo=UTC),
    )

    assert resumed["purged_runs"] == ["old"]
    assert _run_path("old") not in container.values
    assert not any("/parts/old/" in path for path in container.values)
    assert "metadata/oracles/core_nt/gc/old.json" in container.values


def test_purge_status_delete_never_exceeds_blob_budget() -> None:
    container = _Container(
        {
            _run_path("old"): {
                "status": "failed",
                "finished_at": "2026-07-01T00:00:00Z",
            },
            _part_path("old"): "a\n",
        }
    )

    result = purge_oracle_history(
        container,
        db_name="core_nt",
        dry_run=False,
        max_blobs=1,
        now=datetime(2026, 8, 27, tzinfo=UTC),
    )

    assert result["deleted_blobs"] == 1
    assert result["purged_runs"] == []
    assert _run_path("old") in container.values


def test_purge_incrementally_scans_more_than_run_limit() -> None:
    values = {
        _run_path(f"old-{index:03d}"): {
            "status": "failed",
            "finished_at": "2026-07-01T00:00:00Z",
        }
        for index in range(205)
    }
    container = _Container(values)

    result = purge_oracle_history(
        container,
        db_name="core_nt",
        dry_run=True,
        now=datetime(2026, 8, 27, tzinfo=UTC),
    )

    assert result["status"] == "completed"
    assert result["scan_truncated"] is True
    assert result["scanned_runs"] == 50
    assert len(result["planned_runs"]) == 20


def test_truncated_legacy_scan_preserves_all_ready_runs() -> None:
    values = {
        _run_path(f"ready-{index:03d}"): {
            "status": "ready",
            "finished_at": "2026-07-01T00:00:00Z",
        }
        for index in range(205)
    }
    container = _Container(values)

    result = purge_oracle_history(
        container,
        db_name="core_nt",
        dry_run=True,
        now=datetime(2026, 8, 27, tzinfo=UTC),
    )

    assert result["scan_truncated"] is True
    assert result["planned_runs"] == []


def test_retention_cursor_advances_past_referenced_first_page() -> None:
    values: dict[str, object] = {}
    for index in range(205):
        run_id = f"old-{index:03d}"
        values[_run_path(run_id)] = {
            "status": "failed",
            "finished_at": "2026-07-01T00:00:00Z",
        }
        if index < 50:
            values[f"metadata/oracles/core_nt/references/{run_id}/job.json"] = {"run_id": run_id}
    container = _Container(values)

    first = purge_oracle_history(
        container,
        db_name="core_nt",
        dry_run=False,
        now=datetime(2026, 8, 27, tzinfo=UTC),
    )
    second = purge_oracle_history(
        container,
        db_name="core_nt",
        dry_run=True,
        now=datetime(2026, 8, 27, tzinfo=UTC),
    )

    assert first["planned_runs"] == []
    assert first["scan_truncated"] is True
    assert second["planned_runs"] == [f"old-{index:03d}" for index in range(50, 70)]
