"""Tests for Job Artifacts behavior.

Responsibility: Tests for Job Artifacts behavior
Edit boundaries: Keep assertions focused on the behavior under test; prefer fakes over live
Azure calls.
Key entry points: `_state`, `test_build_execution_steps_snapshot_preserves_steps`,
`test_artifact_finalizer_only_runs_for_terminal_phases`,
`test_artifact_finalizer_deduplicates_pending_sentinel`,
`test_artifact_finalizer_waits_for_pending_runtime_identity`,
`test_reconcile_terminal_artifacts_resets_empty_identity_budget`,
`test_finalizer_records_exhausted_pod_log_capture`,
`test_finalizer_records_pod_log_retry_enqueue_failure`,
`test_read_json_artifact_supports_gzip`, `test_artifact_build_should_enqueue_stale_pending`
Risky contracts: Do not require network access or real Azure credentials unless the test is
explicitly integration-scoped.
Validation: `uv run pytest -q api/tests/test_job_artifacts.py`.
"""

from __future__ import annotations

import gzip
import json
from types import SimpleNamespace

import pytest
from api.services import job_artifacts
from api.services.blast.result_artifacts import (
    build_result_aggregate_payload,
    build_result_manifest_payload,
)
from api.services.job_artifacts import ArtifactState, build_execution_steps_snapshot
from api.tasks import blast as blast_tasks
from api.tasks.blast_artifacts import finalize_job_artifacts


def _state(**overrides):
    base = {
        "job_id": "job-1",
        "status": "completed",
        "phase": "completed",
        "created_at": "2026-05-20T00:00:00Z",
        "updated_at": "2026-05-20T00:01:00Z",
        "payload": {
            "_progress": {
                "phase": "completed",
                "status": "completed",
                "steps": {
                    "submitting": {
                        "phase": "submitting",
                        "status": "completed",
                        "last_output": "elastic-blast submitted",
                    }
                },
            }
        },
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_build_execution_steps_snapshot_preserves_steps() -> None:
    snapshot = build_execution_steps_snapshot(_state())

    assert snapshot["job_id"] == "job-1"
    assert snapshot["artifact_state"] == "inline_fallback"
    assert snapshot["custom_status"]["steps"]["submitting"]["last_output"] == (
        "elastic-blast submitted"
    )
    assert snapshot["output"]["steps"]["submitting"]["status"] == "completed"


def test_result_manifest_payload_uses_parseable_result_files(monkeypatch) -> None:
    selected = [
        {
            "name": "2026/08/29/job-1/shard_00/batch_000.out.gz",
            "size": 396,
            "file_id": "result-001",
        }
    ]
    captured: dict[str, object] = {}

    def fake_list(account: str, job_id: str, *, prefix: str | None = None):
        captured.update(account=account, job_id=job_id, prefix=prefix)
        return selected

    monkeypatch.setattr(
        "api.services.blast.result_artifacts.list_parseable_result_blobs",
        fake_list,
    )

    payload = build_result_manifest_payload(
        "job-1",
        "storage1",
        prefix="2026/08/29/job-1/",
    )

    assert captured == {
        "account": "storage1",
        "job_id": "job-1",
        "prefix": "2026/08/29/job-1/",
    }
    assert payload["files"] == selected
    assert payload["manifest"]["file_count"] == 1


def test_build_execution_steps_snapshot_synthesizes_external_row_steps() -> None:
    # A synced `/v1/jobs` row has no dashboard `_progress`; the terminal
    # execution-steps snapshot the SPA overlays must still carry an honest
    # step timeline (not blank), with dashboard-only steps skipped.
    state = _state(
        status="completed",
        phase="completed",
        payload={"external": {"job_id": "ext-9", "status": "success"}},
    )
    snapshot = build_execution_steps_snapshot(state)
    steps = snapshot["output"]["steps"]
    assert snapshot["custom_status"]["steps"] == steps
    assert steps["running"]["status"] == "completed"
    assert steps["warming_up"]["status"] == "skipped"
    assert steps["staging_db"]["status"] == "skipped"


def test_build_execution_steps_snapshot_external_failure_surfaces_error() -> None:
    state = _state(
        status="failed",
        phase="failed",
        payload={"external": {"job_id": "ext-10", "status": "failed"}},
    )
    snapshot = build_execution_steps_snapshot(state)
    assert snapshot["output"]["error"]
    assert "no error detail" in snapshot["output"]["error"].lower()
    assert snapshot["output"]["failed_step"] == "submitting"
    assert snapshot["output"]["steps"]["submitting"]["status"] == "failed"



def test_artifact_finalizer_only_runs_for_terminal_phases(monkeypatch) -> None:
    calls: list[dict[str, str]] = []

    class FakeFinalizer:
        @staticmethod
        def apply_async(*, kwargs, retry):
            assert retry is False
            calls.append(dict(kwargs))

    import api.tasks.blast_artifacts as blast_artifacts

    monkeypatch.setattr(blast_artifacts, "finalize_job_artifacts", FakeFinalizer)
    monkeypatch.setattr(
        job_artifacts,
        "artifact_build_should_enqueue",
        lambda *_args, **_kwargs: True,
    )

    blast_tasks._enqueue_artifact_finalizer("job-1", "submitting", "completed")
    blast_tasks._enqueue_artifact_finalizer("job-1", "completed", "completed")
    blast_tasks._enqueue_artifact_finalizer("job-2", "submit_failed", "failed")

    assert calls == [{"job_id": "job-1"}, {"job_id": "job-2"}]


def test_artifact_finalizer_does_not_open_unused_result_backend() -> None:
    assert finalize_job_artifacts.ignore_result is True


def test_artifact_finalizer_deduplicates_pending_sentinel(monkeypatch) -> None:
    calls: list[dict[str, str]] = []

    class FakeFinalizer:
        @staticmethod
        def apply_async(*, kwargs, retry):
            assert retry is False
            calls.append(dict(kwargs))

    import api.tasks.blast_artifacts as blast_artifacts

    monkeypatch.setattr(blast_artifacts, "finalize_job_artifacts", FakeFinalizer)
    monkeypatch.setattr(
        job_artifacts,
        "artifact_build_should_enqueue",
        lambda *_args, **_kwargs: False,
    )

    blast_tasks._enqueue_artifact_finalizer("job-1", "completed", "completed")

    assert calls == []


def test_artifact_finalizer_marks_enqueue_failure_retryable(monkeypatch) -> None:
    states: list[tuple[str, str]] = []

    class FailingFinalizer:
        @staticmethod
        def apply_async(*, kwargs, retry):
            assert retry is False
            raise ConnectionError(kwargs["job_id"])

    import api.tasks.blast_artifacts as blast_artifacts

    monkeypatch.setattr(blast_artifacts, "finalize_job_artifacts", FailingFinalizer)
    monkeypatch.setattr(job_artifacts, "artifact_build_should_enqueue", lambda *_args: True)
    monkeypatch.setattr(
        job_artifacts,
        "upsert_artifact_state",
        lambda _job_id, _artifact_type, *, status, **kwargs: states.append(
            (status, str(kwargs.get("error_code") or ""))
        ),
    )

    assert blast_tasks._enqueue_artifact_finalizer("job-1", "completed", "completed") is False
    assert states == [("pending", ""), ("failed", "enqueue_failed")]


def test_artifact_finalizer_enqueues_when_dedup_state_is_unavailable(monkeypatch) -> None:
    calls: list[dict[str, str]] = []

    class FakeFinalizer:
        @staticmethod
        def apply_async(*, kwargs, retry):
            assert retry is False
            calls.append(dict(kwargs))

    import api.tasks.blast_artifacts as blast_artifacts

    monkeypatch.setattr(blast_artifacts, "finalize_job_artifacts", FakeFinalizer)
    monkeypatch.setattr(
        job_artifacts,
        "artifact_build_should_enqueue",
        lambda *_args: (_ for _ in ()).throw(ConnectionError("table unavailable")),
    )
    monkeypatch.setattr(
        job_artifacts,
        "upsert_artifact_state",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ConnectionError("table unavailable")),
    )

    assert blast_tasks._enqueue_artifact_finalizer("job-1", "completed", "completed") is True
    assert calls == [{"job_id": "job-1"}]


def test_artifact_finalizer_force_rebuild_bypasses_ready_dedup(monkeypatch) -> None:
    calls: list[dict[str, str]] = []

    class FakeFinalizer:
        @staticmethod
        def apply_async(*, kwargs, retry):
            assert retry is False
            calls.append(dict(kwargs))

    import api.tasks.blast_artifacts as blast_artifacts

    monkeypatch.setattr(blast_artifacts, "finalize_job_artifacts", FakeFinalizer)
    monkeypatch.setattr(
        job_artifacts,
        "artifact_build_should_enqueue",
        lambda *_args: (_ for _ in ()).throw(AssertionError("force must bypass dedup")),
    )
    monkeypatch.setattr(job_artifacts, "upsert_artifact_state", lambda *_args, **_kwargs: None)

    assert blast_tasks._enqueue_artifact_finalizer(
        "job-1", "completed", "completed", force=True
    )
    assert calls == [{"job_id": "job-1"}]


def test_artifact_finalizer_waits_for_pending_runtime_identity(monkeypatch) -> None:
    import api.services.state_repo as state_repo
    import api.tasks.blast_artifacts as blast_artifacts

    state = _state(elastic_blast_job_id="")
    sentinel = ArtifactState(
        job_id="job-1",
        artifact_type="artifact_finalizer",
        status="pending",
        updated_at="2000-01-01T00:00:00+00:00",
        error_code="runtime_identity_pending",
    )
    writes: list[dict[str, object]] = []

    class FakeRepo:
        @staticmethod
        def get(_job_id: str):
            return state

    monkeypatch.setattr(state_repo, "JobStateRepository", lambda: FakeRepo())
    monkeypatch.setattr(job_artifacts, "get_artifact_state", lambda *_args: sentinel)
    monkeypatch.setattr(
        job_artifacts,
        "upsert_artifact_state",
        lambda *_args, **kwargs: writes.append(kwargs),
    )

    result = blast_artifacts.finalize_job_artifacts.run(job_id="job-1")

    assert result["status"] == "identity_pending"
    assert writes == []


def test_reconcile_terminal_artifacts_is_bounded_and_row_isolated(monkeypatch) -> None:
    import api.services.state_repo as state_repo
    import api.tasks.blast.state as blast_state
    import api.tasks.blast_artifacts as blast_artifacts

    rows = [
        _state(job_id="job-1"),
        _state(job_id="job-2", status="failed", phase="failed"),
        _state(job_id="job-3", status="cancelled", phase="cancelled"),
    ]
    calls: list[tuple[str, int, int]] = []

    class FakeRepo:
        def list_recent_terminal(
            self,
            *,
            job_type,
            limit,
            since_seconds,
            include_payload,
        ):
            assert include_payload is False
            calls.append((job_type, limit, since_seconds))
            return rows

    def fake_enqueue(
        job_id,
        _phase,
        _status,
        *,
        runtime_identity="",
        reconcile_attempts=0,
    ):
        assert runtime_identity == getattr(
            next(row for row in rows if row.job_id == job_id),
            "elastic_blast_job_id",
            "",
        )
        assert reconcile_attempts == 1
        if job_id == "job-2":
            raise RuntimeError("row-local")
        return job_id == "job-1"

    monkeypatch.setattr(state_repo, "JobStateRepository", lambda: FakeRepo())
    monkeypatch.setattr(blast_state, "_enqueue_artifact_finalizer", fake_enqueue)
    monkeypatch.setattr(job_artifacts, "get_artifact_state", lambda *_args: None)

    summary = blast_artifacts.reconcile_terminal_artifacts.run(
        limit=10_000,
        since_seconds=99 * 86_400,
    )

    assert calls == [("blast", 500, 7 * 86_400)]
    assert summary == {"scanned": 3, "enqueued": 1, "errors": 1}


def test_reconcile_terminal_artifacts_stops_after_generation_budget(monkeypatch) -> None:
    import api.services.state_repo as state_repo
    import api.tasks.blast.state as blast_state
    import api.tasks.blast_artifacts as blast_artifacts

    runtime_identity = "job-11111111111111111111111111111111"
    row = _state(job_id="job-1", elastic_blast_job_id=runtime_identity)
    sentinel = ArtifactState(
        job_id="job-1",
        artifact_type="artifact_finalizer",
        status="failed",
        runtime_identity=runtime_identity,
        reconcile_attempts=blast_artifacts._RECONCILE_ATTEMPT_MAX,
    )

    class FakeRepo:
        @staticmethod
        def list_recent_terminal(**kwargs):
            assert kwargs["include_payload"] is False
            return [row]

    monkeypatch.setattr(state_repo, "JobStateRepository", lambda: FakeRepo())
    monkeypatch.setattr(job_artifacts, "get_artifact_state", lambda *_args: sentinel)
    monkeypatch.setattr(
        blast_state,
        "_enqueue_artifact_finalizer",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("budget exhausted")),
    )

    summary = blast_artifacts.reconcile_terminal_artifacts.run()

    assert summary == {"scanned": 1, "enqueued": 0, "errors": 0}


def test_reconcile_terminal_artifacts_resets_budget_for_new_identity(monkeypatch) -> None:
    import api.services.state_repo as state_repo
    import api.tasks.blast.state as blast_state
    import api.tasks.blast_artifacts as blast_artifacts

    row = _state(
        job_id="job-1",
        elastic_blast_job_id="job-22222222222222222222222222222222",
    )
    sentinel = ArtifactState(
        job_id="job-1",
        artifact_type="artifact_finalizer",
        status="ready",
        runtime_identity="job-11111111111111111111111111111111",
        reconcile_attempts=blast_artifacts._RECONCILE_ATTEMPT_MAX,
    )
    calls: list[dict[str, object]] = []

    class FakeRepo:
        @staticmethod
        def list_recent_terminal(**kwargs):
            assert kwargs["include_payload"] is False
            return [row]

    def fake_enqueue(*_args, **kwargs):
        calls.append(kwargs)
        return True

    monkeypatch.setattr(state_repo, "JobStateRepository", lambda: FakeRepo())
    monkeypatch.setattr(job_artifacts, "get_artifact_state", lambda *_args: sentinel)
    monkeypatch.setattr(blast_state, "_enqueue_artifact_finalizer", fake_enqueue)

    summary = blast_artifacts.reconcile_terminal_artifacts.run()

    assert summary == {"scanned": 1, "enqueued": 1, "errors": 0}
    assert calls == [
        {
            "runtime_identity": "job-22222222222222222222222222222222",
            "reconcile_attempts": 1,
        }
    ]


def test_reconcile_terminal_artifacts_resets_empty_identity_budget(monkeypatch) -> None:
    import api.services.state_repo as state_repo
    import api.tasks.blast.state as blast_state
    import api.tasks.blast_artifacts as blast_artifacts

    runtime_identity = "job-22222222222222222222222222222222"
    row = _state(job_id="job-1", elastic_blast_job_id=runtime_identity)
    sentinel = ArtifactState(
        job_id="job-1",
        artifact_type="artifact_finalizer",
        status="pending",
        runtime_identity="",
        reconcile_attempts=blast_artifacts._RECONCILE_ATTEMPT_MAX,
    )
    calls: list[dict[str, object]] = []

    class FakeRepo:
        @staticmethod
        def list_recent_terminal(**_kwargs):
            return [row]

    monkeypatch.setattr(state_repo, "JobStateRepository", lambda: FakeRepo())
    monkeypatch.setattr(job_artifacts, "get_artifact_state", lambda *_args: sentinel)
    monkeypatch.setattr(
        blast_state,
        "_enqueue_artifact_finalizer",
        lambda *_args, **kwargs: calls.append(kwargs) or True,
    )

    summary = blast_artifacts.reconcile_terminal_artifacts.run()

    assert summary == {"scanned": 1, "enqueued": 1, "errors": 0}
    assert calls == [{"runtime_identity": runtime_identity, "reconcile_attempts": 1}]


def test_finalizer_records_exhausted_pod_log_capture(monkeypatch) -> None:
    import api.services.job_logs.persist as log_persist
    import api.services.state_repo as state_repo
    import api.tasks.blast_artifacts as blast_artifacts

    runtime_identity = "job-22222222222222222222222222222222"
    state = _state(
        status="failed",
        phase="failed",
        storage_account="",
        elastic_blast_job_id=runtime_identity,
    )
    writes: list[tuple[str, dict[str, object]]] = []
    history: list[tuple[str, str, dict[str, object]]] = []

    class FakeRepo:
        @staticmethod
        def get(_job_id: str):
            return state

        @staticmethod
        def append_history(job_id: str, event: str, payload: dict[str, object]) -> None:
            history.append((job_id, event, payload))

    monkeypatch.setattr(state_repo, "JobStateRepository", lambda: FakeRepo())
    monkeypatch.setattr(job_artifacts, "get_artifact_state", lambda *_args: None)
    monkeypatch.setattr(
        job_artifacts,
        "upsert_artifact_state",
        lambda _job_id, artifact_type, **kwargs: writes.append((artifact_type, kwargs)),
    )
    monkeypatch.setattr(job_artifacts, "write_execution_steps_snapshot", lambda *_args: {})
    monkeypatch.setattr(log_persist, "persist_completed_job_pod_logs", lambda *_args: {})

    result = blast_artifacts.finalize_job_artifacts.run(
        job_id="job-1",
        pod_log_attempt=blast_artifacts._POD_LOG_RETRY_MAX,
    )

    assert result["status"] == "completed"
    assert result["pod_logs_error"] == "capture_exhausted"
    assert (
        "pod_logs",
        {
            "status": "failed",
            "error_code": "capture_exhausted",
            "runtime_identity": runtime_identity,
            "reconcile_attempts": blast_artifacts._POD_LOG_RETRY_MAX,
        },
    ) in writes
    assert history == [
        (
            "job-1",
            "pod_logs_capture_failed",
            {
                "attempt": blast_artifacts._POD_LOG_RETRY_MAX,
                "error_code": "capture_exhausted",
                "runtime_identity": runtime_identity,
            },
        )
    ]


def test_finalizer_records_pod_log_retry_enqueue_failure(monkeypatch) -> None:
    import api.services.job_logs.persist as log_persist
    import api.services.state_repo as state_repo
    import api.tasks.blast_artifacts as blast_artifacts

    runtime_identity = "job-22222222222222222222222222222222"
    state = _state(
        status="failed",
        phase="failed",
        storage_account="",
        elastic_blast_job_id=runtime_identity,
    )
    writes: list[tuple[str, dict[str, object]]] = []
    history: list[tuple[str, str, dict[str, object]]] = []

    class FakeRepo:
        @staticmethod
        def get(_job_id: str):
            return state

        @staticmethod
        def append_history(job_id: str, event: str, payload: dict[str, object]) -> None:
            history.append((job_id, event, payload))

    monkeypatch.setattr(state_repo, "JobStateRepository", lambda: FakeRepo())
    monkeypatch.setattr(job_artifacts, "get_artifact_state", lambda *_args: None)
    monkeypatch.setattr(
        job_artifacts,
        "upsert_artifact_state",
        lambda _job_id, artifact_type, **kwargs: writes.append((artifact_type, kwargs)),
    )
    monkeypatch.setattr(job_artifacts, "write_execution_steps_snapshot", lambda *_args: {})
    monkeypatch.setattr(log_persist, "persist_completed_job_pod_logs", lambda *_args: {})
    monkeypatch.setattr(
        blast_artifacts.finalize_job_artifacts,
        "apply_async",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("broker unavailable")),
    )

    result = blast_artifacts.finalize_job_artifacts.run(job_id="job-1")

    assert result["status"] == "completed"
    assert result["pod_logs_error"] == "retry_enqueue_failed"
    assert (
        "pod_logs",
        {
            "status": "failed",
            "error_code": "retry_enqueue_failed",
            "runtime_identity": runtime_identity,
            "reconcile_attempts": 1,
        },
    ) in writes
    assert history == [
        (
            "job-1",
            "pod_logs_capture_failed",
            {
                "attempt": 1,
                "error_code": "retry_enqueue_failed",
                "runtime_identity": runtime_identity,
            },
        )
    ]


def test_read_json_artifact_supports_gzip(monkeypatch) -> None:
    body = gzip.compress(b'{"ok":true}')
    state = ArtifactState(
        job_id="job-1",
        artifact_type="result_aggregate",
        status="ready",
        blob_path="job-1/results/aggregate.json.gz",
    )

    monkeypatch.setenv("AZURE_BLOB_ENDPOINT", "https://acct.blob.core.windows.net")
    monkeypatch.setattr(job_artifacts, "get_artifact_state", lambda *_args: state)
    monkeypatch.setattr(job_artifacts, "get_credential", lambda: object())
    monkeypatch.setattr(job_artifacts.storage_data, "stream_blob_bytes", lambda *_args: [body])

    assert job_artifacts.read_json_artifact("job-1", "result_aggregate") == {"ok": True}


def test_read_json_artifact_marks_failed_when_blob_missing(monkeypatch) -> None:
    """A "ready" state whose blob is gone must flip to "failed" (so a rebuild is
    enqueued) and return None instead of raising the 404 to the caller."""
    from azure.core.exceptions import ResourceNotFoundError

    state = ArtifactState(
        job_id="job-1",
        artifact_type="result_aggregate",
        status="ready",
        blob_path="job-1/results/aggregate.json",
    )
    upserts: list[dict[str, str]] = []

    def _boom(*_args, **_kwargs):
        raise ResourceNotFoundError("blob not found")

    monkeypatch.setenv("AZURE_BLOB_ENDPOINT", "https://acct.blob.core.windows.net")
    monkeypatch.setattr(job_artifacts, "get_artifact_state", lambda *_args: state)
    monkeypatch.setattr(job_artifacts, "get_credential", lambda: object())
    monkeypatch.setattr(job_artifacts.storage_data, "read_blob_text", _boom)
    monkeypatch.setattr(
        job_artifacts,
        "upsert_artifact_state",
        lambda job_id, artifact_type, **kw: upserts.append(
            {"job_id": job_id, "type": artifact_type, **kw}
        ),
    )

    assert job_artifacts.read_json_artifact("job-1", "result_aggregate") is None
    assert upserts == [
        {
            "job_id": "job-1",
            "type": "result_aggregate",
            "status": "failed",
            "error_code": "blob_missing",
        }
    ]


def test_read_json_artifact_marks_failed_when_gzip_blob_missing(monkeypatch) -> None:
    """Same recovery for the gzip path (stream_blob_bytes raises)."""
    from azure.core.exceptions import ResourceNotFoundError

    state = ArtifactState(
        job_id="job-1",
        artifact_type="result_aggregate",
        status="ready",
        blob_path="job-1/results/aggregate.json.gz",
    )
    upserts: list[dict[str, str]] = []

    def _boom(*_args, **_kwargs):
        raise ResourceNotFoundError("blob not found")

    monkeypatch.setenv("AZURE_BLOB_ENDPOINT", "https://acct.blob.core.windows.net")
    monkeypatch.setattr(job_artifacts, "get_artifact_state", lambda *_args: state)
    monkeypatch.setattr(job_artifacts, "get_credential", lambda: object())
    monkeypatch.setattr(job_artifacts.storage_data, "stream_blob_bytes", _boom)
    monkeypatch.setattr(
        job_artifacts,
        "upsert_artifact_state",
        lambda job_id, artifact_type, **kw: upserts.append(
            {"job_id": job_id, "type": artifact_type, **kw}
        ),
    )

    assert job_artifacts.read_json_artifact("job-1", "result_aggregate") is None
    assert upserts and upserts[0]["status"] == "failed"
    assert upserts[0]["error_code"] == "blob_missing"


def test_artifact_build_should_enqueue_stale_pending(monkeypatch) -> None:
    fresh = ArtifactState(
        job_id="job-1",
        artifact_type="result_aggregate",
        status="pending",
        updated_at="2999-01-01T00:00:00+00:00",
    )
    stale = ArtifactState(
        job_id="job-1",
        artifact_type="result_aggregate",
        status="pending",
        updated_at="2000-01-01T00:00:00+00:00",
    )

    monkeypatch.setattr(job_artifacts, "get_artifact_state", lambda *_args: fresh)
    assert job_artifacts.artifact_build_should_enqueue("job-1", ["result_aggregate"]) is False

    monkeypatch.setattr(job_artifacts, "get_artifact_state", lambda *_args: stale)
    assert job_artifacts.artifact_build_should_enqueue("job-1", ["result_aggregate"]) is True


def test_artifact_build_does_not_expire_runtime_identity_pending(monkeypatch) -> None:
    pending = ArtifactState(
        job_id="job-1",
        artifact_type="artifact_finalizer",
        status="pending",
        updated_at="2000-01-01T00:00:00+00:00",
        error_code="runtime_identity_pending",
    )

    monkeypatch.setattr(job_artifacts, "get_artifact_state", lambda *_args: pending)

    assert job_artifacts.artifact_build_should_enqueue("job-1", ["artifact_finalizer"]) is False


def test_artifact_build_rebuilds_when_runtime_identity_changes(monkeypatch) -> None:
    ready = ArtifactState(
        job_id="job-1",
        artifact_type="artifact_finalizer",
        status="ready",
        runtime_identity="job-11111111111111111111111111111111",
    )
    monkeypatch.setattr(job_artifacts, "get_artifact_state", lambda *_args: ready)

    assert not job_artifacts.artifact_build_should_enqueue(
        "job-1",
        ["artifact_finalizer"],
        runtime_identity="job-11111111111111111111111111111111",
    )
    assert job_artifacts.artifact_build_should_enqueue(
        "job-1",
        ["artifact_finalizer"],
        runtime_identity="job-22222222222222222222222222222222",
    )


def test_artifact_build_does_not_duplicate_active_unknown_identity_finalizer(
    monkeypatch,
) -> None:
    pending = ArtifactState(
        job_id="job-1",
        artifact_type="artifact_finalizer",
        status="pending",
        updated_at="2999-01-01T00:00:00+00:00",
    )
    monkeypatch.setattr(job_artifacts, "get_artifact_state", lambda *_args: pending)

    assert not job_artifacts.artifact_build_should_enqueue(
        "job-1",
        ["artifact_finalizer"],
        runtime_identity="job-22222222222222222222222222222222",
    )


def test_artifact_build_wakes_identity_pending_after_identity_arrives(monkeypatch) -> None:
    pending = ArtifactState(
        job_id="job-1",
        artifact_type="artifact_finalizer",
        status="pending",
        updated_at="2999-01-01T00:00:00+00:00",
        error_code="runtime_identity_pending",
    )
    monkeypatch.setattr(job_artifacts, "get_artifact_state", lambda *_args: pending)

    assert job_artifacts.artifact_build_should_enqueue(
        "job-1",
        ["artifact_finalizer"],
        runtime_identity="job-22222222222222222222222222222222",
    )


def test_write_execution_log_chunk_uses_safe_paths(monkeypatch) -> None:
    calls: list[tuple[str, str, str, dict]] = []

    def fake_write(job_id, artifact_type, suffix, payload):
        calls.append((job_id, artifact_type, suffix, payload))
        return ArtifactState(job_id=job_id, artifact_type=artifact_type, status="ready")

    monkeypatch.setattr(job_artifacts, "write_json_artifact", fake_write)

    job_artifacts.write_execution_log_chunk(
        "job-1",
        "submit/log",
        7,
        [{"stream": "stdout", "line": "hello", "index": 1}],
    )

    assert calls[0][1] == "execution_log_submit_log_000007"
    assert calls[0][2] == "execution-steps/logs/submit_log/000007.json"
    assert calls[0][3]["events"][0]["line"] == "hello"


def test_streaming_aggregate_does_not_hit_cap(monkeypatch) -> None:
    rows = [
        f"query{i}\tNC_{i}\t99.0\t100\t0\t0\t1\t100\t1\t100\t1e-20\t{i}"
        for i in range(3)
    ]

    monkeypatch.setattr(
        "api.services.blast.result_artifacts.list_parseable_result_blobs",
        lambda *_args: [{"name": "job-1/out.tsv"}],
    )
    monkeypatch.setattr(
        "api.services.blast.result_artifacts.storage_data.read_result_blob_text",
        lambda *_args, **_kwargs: "\n".join(rows),
    )
    monkeypatch.setattr("api.services.blast.result_artifacts.get_credential", lambda: object())

    payload = build_result_aggregate_payload("job-1", "acct")

    assert payload["stats"]["total_hits"] == 3
    assert payload["stats"]["unique_queries"] == 3
    assert payload["truncated"] is False


def _patch_merge_report(monkeypatch, text):
    """Install a fake results-container reader returning ``text`` for the
    merge-report blob (or raising when ``text`` is an Exception)."""

    def _read(_cred, _account, container, blob_path, max_bytes=4096):
        assert container == "results"
        assert blob_path.endswith("/merge-report.json")
        if isinstance(text, Exception):
            raise text
        return text

    monkeypatch.setattr(
        "api.services.blast.result_artifacts.storage_data.read_blob_text", _read
    )
    monkeypatch.setattr("api.services.blast.result_artifacts.get_credential", lambda: object())


def test_load_merge_report_tie_cutoff_summarizes_overflow(monkeypatch) -> None:
    from api.services.blast import result_artifacts

    report = {
        "tie_cutoff_overflow_count": 4,
        "diversity_reserved_count": 0,
        "max_target_seqs": 500,
        "tie_cutoff_queries": [
            {"query_id": "q1", "overflow_count": 4},
            {"query_id": "q2", "overflow_count": 1},
            {"query_id": "q3"},
            {"query_id": "q4"},
            {"query_id": "q5"},
            {"query_id": "q6"},
        ],
    }
    _patch_merge_report(monkeypatch, json.dumps(report))

    summary = result_artifacts._load_merge_report_tie_cutoff("job-1", "acct")

    assert summary == {
        "overflow_count": 4,
        "diversity_reserved_count": 0,
        "max_target_seqs": 500,
        "queries": report["tie_cutoff_queries"][:5],
    }


def test_load_merge_report_tie_cutoff_omits_when_no_signal(monkeypatch) -> None:
    from api.services.blast import result_artifacts

    _patch_merge_report(
        monkeypatch,
        json.dumps({"tie_cutoff_overflow_count": 0, "diversity_reserved_count": 0}),
    )

    assert result_artifacts._load_merge_report_tie_cutoff("job-1", "acct") is None


def test_load_merge_report_tie_cutoff_tolerates_missing_report(monkeypatch) -> None:
    from api.services.blast import result_artifacts

    _patch_merge_report(monkeypatch, RuntimeError("blob not found"))

    assert result_artifacts._load_merge_report_tie_cutoff("job-1", "acct") is None


def test_load_merge_report_tie_cutoff_tolerates_malformed_json(monkeypatch) -> None:
    from api.services.blast import result_artifacts

    _patch_merge_report(monkeypatch, "{not valid json")

    assert result_artifacts._load_merge_report_tie_cutoff("job-1", "acct") is None


def test_load_merge_report_tie_cutoff_reports_diversity_only(monkeypatch) -> None:
    from api.services.blast import result_artifacts

    _patch_merge_report(
        monkeypatch,
        json.dumps({"tie_cutoff_overflow_count": 0, "diversity_reserved_count": 2}),
    )

    summary = result_artifacts._load_merge_report_tie_cutoff("job-1", "acct")

    assert summary == {
        "overflow_count": 0,
        "diversity_reserved_count": 2,
        "queries": [],
    }


def test_worker_command_rejects_untrusted_queue_values() -> None:
    from api import run_celery_workers

    with pytest.raises(ValueError, match="invalid queues"):
        run_celery_workers._worker_command("worker-main", "default;touch x", "1")

    with pytest.raises(ValueError, match="invalid concurrency"):
        run_celery_workers._worker_command("worker-main", "default", "0")


def test_worker_command_default_pool_is_prefork() -> None:
    """prefork is the safety default — ARM pollers rely on its signal-based
    hard time limit. The command must request it explicitly and must NOT add
    a --max-memory-per-child flag unless one is configured."""
    from api import run_celery_workers

    cmd = run_celery_workers._worker_command(
        "worker-main", "default,reconcile", "4", max_memory_per_child_kb=""
    )
    assert "--pool" in cmd
    assert cmd[cmd.index("--pool") + 1] == "prefork"
    assert "--max-memory-per-child" not in cmd


def test_worker_command_honours_pool_and_memory_backstop() -> None:
    from api import run_celery_workers

    cmd = run_celery_workers._worker_command(
        "worker-main", "default", "2", pool="prefork", max_memory_per_child_kb="500000"
    )
    assert cmd[cmd.index("--max-memory-per-child") + 1] == "500000"

    # "0" disables the backstop rather than being passed through.
    cmd_zero = run_celery_workers._worker_command(
        "worker-main", "default", "2", pool="prefork", max_memory_per_child_kb="0"
    )
    assert "--max-memory-per-child" not in cmd_zero

    # Non-prefork pools never get the prefork-only flag.
    cmd_threads = run_celery_workers._worker_command(
        "worker-main", "default", "2", pool="threads", max_memory_per_child_kb="500000"
    )
    assert cmd_threads[cmd_threads.index("--pool") + 1] == "threads"
    assert "--max-memory-per-child" not in cmd_threads


def test_worker_command_rejects_untrusted_pool_and_memory() -> None:
    from api import run_celery_workers

    with pytest.raises(ValueError, match="invalid pool"):
        run_celery_workers._worker_command("worker-main", "default", "1", pool="prefork; rm -rf")

    with pytest.raises(ValueError, match="invalid max-memory-per-child"):
        run_celery_workers._worker_command(
            "worker-main", "default", "1", max_memory_per_child_kb="9; touch x"
        )


def test_read_result_analytics_artifact_treats_missing_schema_as_stale(monkeypatch) -> None:
    """A baked payload without `artifact_schema_version` (i.e. from a
    pre-v2 code version) must be returned as None, AND the state row
    must be flipped to `failed` so the next request triggers a rebuild.
    Locks in the auto-invalidation contract that backs the
    2026-05-22 Descriptions / Taxonomy fast-path fix."""
    stale_payload = {
        "job_id": "job-1",
        "organisms": [{"key": "unclassified", "organism": "", "count": 100}],
        # No `artifact_schema_version` field — simulates a v1 payload
        # written by a pre-Phase-2 worker.
    }
    monkeypatch.setattr(
        job_artifacts,
        "read_json_artifact",
        lambda *_args, **_kwargs: stale_payload,
    )
    upserts: list[dict[str, str]] = []
    monkeypatch.setattr(
        job_artifacts,
        "upsert_artifact_state",
        lambda *args, **kwargs: upserts.append({**kwargs, "args": list(args)}),
    )

    result = job_artifacts.read_result_analytics_artifact("job-1", "result_taxonomy")

    assert result is None
    assert len(upserts) == 1
    assert upserts[0]["status"] == "failed"
    assert upserts[0]["error_code"] == "schema_stale"


def test_read_result_analytics_artifact_returns_fresh_payload(monkeypatch) -> None:
    """Payloads that meet the schema floor pass through unchanged and
    the state row is left alone."""
    fresh_payload = {
        "artifact_schema_version": 4,
        "job_id": "job-1",
        "organisms": [
            {
                "key": "monkeypox virus",
                "organism": "Monkeypox virus",
                "count": 100,
                "blast_name": "viruses",
            }
        ],
    }
    monkeypatch.setattr(
        job_artifacts,
        "read_json_artifact",
        lambda *_args, **_kwargs: fresh_payload,
    )
    upserts: list[object] = []
    monkeypatch.setattr(
        job_artifacts,
        "upsert_artifact_state",
        lambda *args, **kwargs: upserts.append((args, kwargs)),
    )

    result = job_artifacts.read_result_analytics_artifact("job-1", "result_taxonomy")

    assert result == fresh_payload
    assert upserts == []


def test_read_result_aggregate_v3_is_stale_after_prefix_fallback_fix(
    monkeypatch,
) -> None:
    stale_payload = {
        "artifact_schema_version": 3,
        "job_id": "external-job-1",
        "status": "no_hits",
        "files_parsed": 0,
        "total_files": 0,
    }
    monkeypatch.setattr(
        job_artifacts,
        "read_json_artifact",
        lambda *_args, **_kwargs: stale_payload,
    )
    upserts: list[dict[str, object]] = []
    monkeypatch.setattr(
        job_artifacts,
        "upsert_artifact_state",
        lambda *args, **kwargs: upserts.append({**kwargs, "args": list(args)}),
    )

    result = job_artifacts.read_result_analytics_artifact(
        "external-job-1", "result_aggregate"
    )

    assert result is None
    assert upserts[0]["status"] == "failed"
    assert upserts[0]["error_code"] == "schema_stale"


def test_read_result_manifest_without_schema_is_stale_after_prefix_fallback_fix(
    monkeypatch,
) -> None:
    stale_payload = {
        "job_id": "external-job-1",
        "files": [],
        "results": [],
    }
    monkeypatch.setattr(
        job_artifacts,
        "read_json_artifact",
        lambda *_args, **_kwargs: stale_payload,
    )
    upserts: list[dict[str, object]] = []
    monkeypatch.setattr(
        job_artifacts,
        "upsert_artifact_state",
        lambda *args, **kwargs: upserts.append({**kwargs, "args": list(args)}),
    )

    result = job_artifacts.read_result_analytics_artifact(
        "external-job-1", "result_manifest"
    )

    assert result is None
    assert upserts[0]["status"] == "failed"
    assert upserts[0]["error_code"] == "schema_stale"
