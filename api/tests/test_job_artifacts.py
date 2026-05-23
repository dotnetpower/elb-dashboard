"""Tests for Job Artifacts behavior.

Responsibility: Tests for Job Artifacts behavior
Edit boundaries: Keep assertions focused on the behavior under test; prefer fakes over live
Azure calls.
Key entry points: `_state`, `test_build_execution_steps_snapshot_preserves_steps`,
`test_artifact_finalizer_only_runs_for_terminal_phases`,
`test_artifact_finalizer_deduplicates_pending_sentinel`,
`test_read_json_artifact_supports_gzip`, `test_artifact_build_should_enqueue_stale_pending`
Risky contracts: Do not require network access or real Azure credentials unless the test is
explicitly integration-scoped.
Validation: `uv run pytest -q api/tests/test_job_artifacts.py`.
"""

from __future__ import annotations

import gzip
from types import SimpleNamespace

import pytest
from api.services import job_artifacts
from api.services.blast.result_artifacts import build_result_aggregate_payload
from api.services.job_artifacts import ArtifactState, build_execution_steps_snapshot
from api.tasks import blast as blast_tasks


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


def test_artifact_finalizer_only_runs_for_terminal_phases(monkeypatch) -> None:
    calls: list[dict[str, str]] = []

    class FakeFinalizer:
        @staticmethod
        def apply_async(*, kwargs):
            calls.append(dict(kwargs))

    import api.tasks.blast_artifacts as blast_artifacts

    monkeypatch.setattr(blast_artifacts, "finalize_job_artifacts", FakeFinalizer)
    monkeypatch.setattr(job_artifacts, "artifact_build_should_enqueue", lambda *_args: True)

    blast_tasks._enqueue_artifact_finalizer("job-1", "submitting", "completed")
    blast_tasks._enqueue_artifact_finalizer("job-1", "completed", "completed")
    blast_tasks._enqueue_artifact_finalizer("job-2", "submit_failed", "failed")

    assert calls == [{"job_id": "job-1"}, {"job_id": "job-2"}]


def test_artifact_finalizer_deduplicates_pending_sentinel(monkeypatch) -> None:
    calls: list[dict[str, str]] = []

    class FakeFinalizer:
        @staticmethod
        def apply_async(*, kwargs):
            calls.append(dict(kwargs))

    import api.tasks.blast_artifacts as blast_artifacts

    monkeypatch.setattr(blast_artifacts, "finalize_job_artifacts", FakeFinalizer)
    monkeypatch.setattr(job_artifacts, "artifact_build_should_enqueue", lambda *_args: False)

    blast_tasks._enqueue_artifact_finalizer("job-1", "completed", "completed")

    assert calls == []


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


def test_worker_command_rejects_untrusted_queue_values() -> None:
    from api import run_celery_workers

    with pytest.raises(ValueError, match="invalid queues"):
        run_celery_workers._worker_command("worker-main", "default;touch x", "1")

    with pytest.raises(ValueError, match="invalid concurrency"):
        run_celery_workers._worker_command("worker-main", "default", "0")


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
        "artifact_schema_version": 2,
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
