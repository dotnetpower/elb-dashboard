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
import json
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
