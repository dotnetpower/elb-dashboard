"""Unit tests for the external (OpenAPI) job → dashboard projection.

Module summary: Drives `_external_to_blast_job` with raw OpenAPI job dicts to
pin the response contract the SPA's `BlastJobSummary` type depends on.

Responsibility: Verify the projected ``job_id`` is always a string (never None)
  so SPA navigation/keys never produce ``/blast/jobs/null``.
Edit boundaries: Pure projection — no Azure calls.
Key entry points: `test_external_job_id_is_always_a_string`.
Risky contracts: `BlastJobSummary.job_id` is a non-nullable string in
  `web/src/api/blast.types.ts`.
Validation: `uv run pytest -q api/tests/test_external_job_projection.py`.
"""

from __future__ import annotations

from api.services.blast.external_job_projection import _external_to_blast_job


def test_external_job_id_is_always_a_string_when_present() -> None:
    out = _external_to_blast_job({"job_id": "abc123", "status": "running"})
    assert out["job_id"] == "abc123"
    assert out["job_id_kind"] == "openapi"


def test_external_job_id_falls_back_to_empty_string_when_missing() -> None:
    """If the upstream OpenAPI response omits ``job_id``, the projection must
    emit an empty string, not ``None`` — the SPA's BlastJobSummary.job_id type
    is a non-nullable string and ``None`` would render ``/blast/jobs/null``."""
    out = _external_to_blast_job({"status": "queued"})
    assert out["job_id"] == ""
    assert isinstance(out["job_id"], str)


def _steps(out: dict) -> dict:
    steps = out["output"]["steps"]
    # custom_status mirrors the same steps map the SPA timeline reads.
    assert out["custom_status"]["steps"] == steps
    return steps


def test_external_steps_present_for_running_job() -> None:
    out = _external_to_blast_job({"job_id": "j1", "status": "running"})
    steps = _steps(out)
    # Universally-true lifecycle steps are marked done; the run is active.
    assert steps["preparing"]["status"] == "completed"
    assert steps["configuring"]["status"] == "completed"
    assert steps["submitting"]["status"] == "completed"
    assert steps["running"]["status"] == "running"


def test_external_dashboard_only_steps_are_skipped_not_faked() -> None:
    # The sibling never runs the dashboard's node-local warmup / SSD-staging,
    # so a completed external job must show those skipped — NOT a fake "done".
    out = _external_to_blast_job({"job_id": "j2", "status": "success"})
    steps = _steps(out)
    assert steps["warming_up"]["status"] == "skipped"
    assert steps["warming_up"]["skip_reason"] == "not_reported_by_external_api"
    assert steps["staging_db"]["status"] == "skipped"
    # The real lifecycle steps are completed.
    assert steps["completed"]["status"] == "completed"
    assert steps["running"]["status"] == "completed"


def test_external_failed_job_attaches_error_to_submit_step() -> None:
    out = _external_to_blast_job(
        {"job_id": "j3", "status": "failed", "error": {"message": "db not found"}}
    )
    steps = _steps(out)
    assert out["output"]["failed_step"] == "submitting"
    assert "db not found" in out["output"]["error"]
    assert "db not found" in out["error"]
    assert steps["submitting"]["status"] == "failed"
    assert steps["submitting"]["success"] is False
    assert "db not found" in steps["submitting"]["error"]


def test_external_failed_job_without_detail_gets_honest_fallback() -> None:
    # Regression: a sibling failure with no error body previously left the
    # banner showing "No detailed error was recorded".
    out = _external_to_blast_job({"job_id": "j4", "status": "failed"})
    assert out["error"]
    assert "no error detail" in out["error"].lower()
    assert out["output"]["failed_step"] == "submitting"


def test_external_failed_during_run_blames_running_step() -> None:
    # Visible shard activity means the failure happened during the BLAST run,
    # not at submit time → blame the running step.
    out = _external_to_blast_job(
        {
            "job_id": "j5",
            "status": "failed",
            "error": "shard crashed",
            "execution": {"shard_count": 4, "shards_failed": 1, "shards_succeeded": 3},
        }
    )
    assert out["output"]["failed_step"] == "running"
    assert out["output"]["steps"]["submitting"]["status"] == "completed"
    assert out["output"]["steps"]["running"]["status"] == "failed"


def test_external_completed_job_surfaces_real_execution_detail() -> None:
    out = _external_to_blast_job(
        {
            "job_id": "j6",
            "status": "success",
            "blast_version": "BLASTN 2.17.0+",
            "db_version": "core_nt 2026-05",
            "result": {"hit_count": 42},
            "execution": {"shard_count": 2, "shards_succeeded": 2},
        }
    )
    detail = out["output"]["steps"]["running"]["last_output"]
    assert "BLASTN 2.17.0+" in detail
    assert "core_nt 2026-05" in detail
    assert "42" in detail


def test_external_queued_job_marks_prepare_running() -> None:
    out = _external_to_blast_job({"job_id": "j7", "status": "queued"})
    steps = _steps(out)
    assert steps["preparing"]["status"] == "running"
    # Nothing downstream is fabricated as done yet — submit/run are not even
    # present (the frontend treats a missing step as pending).
    assert steps.get("submitting") is None
    assert steps.get("running") is None
    # Dashboard-only steps are skipped even while queued.
    assert steps["warming_up"]["status"] == "skipped"


def test_external_cancelled_job_is_not_a_failure() -> None:
    out = _external_to_blast_job({"job_id": "j8", "status": "cancelled"})
    steps = _steps(out)
    # Cancelled is terminal but NOT a failure: no failed_step, the stopped
    # step is skipped with a cancellation reason, no synthesized error.
    assert out["output"].get("failed_step") is None
    assert "error" not in out
    assert steps["submitting"]["status"] == "skipped"
    assert steps["submitting"]["skip_reason"] == "cancelled"
    assert steps["preparing"]["status"] == "completed"


def test_external_error_message_is_sanitised() -> None:
    # A sibling failure body can leak a SAS token / bearer / subscription GUID.
    # The projected message (banner + failed-step error) must be sanitised
    # before it reaches the UI (Charter §12).
    out = _external_to_blast_job(
        {
            "job_id": "j9",
            "status": "failed",
            "error": (
                "upload failed: https://acct.blob.core.windows.net/c/b"
                "?sv=2021&sig=AAAABBBBCCCCsecretsig%3D used "
                "Bearer eyJhbGciOi.JIUzI1NiIsInR5cCI6.IkpXVCJ9abcdef"
            ),
        }
    )
    message = out["error"]
    assert "sig=AAAABBBBCCCC" not in message
    assert "secretsig" not in message
    assert "<sas-redacted>" in message or "sig=<redacted>" in message
    assert "Bearer <redacted>" in message
    # The same sanitised text is on the failed step.
    assert out["output"]["steps"]["submitting"]["error"] == message


def test_external_failed_job_enriched_with_cluster_detail(monkeypatch) -> None:
    # A sibling failure with a generic/empty error: on the detail view the
    # dashboard recovers the authoritative blastn detail from the results
    # container and replaces the placeholder banner + failed-step error.
    import api.services.blast.runtime_failure as runtime_failure

    monkeypatch.setattr(
        runtime_failure,
        "read_blast_runtime_failure",
        lambda account, job_id: "BLAST search exited with code 2: bad alphabet",
    )
    out = _external_to_blast_job(
        {
            "job_id": "ext-1",
            "status": "failed",
            "error": "one or more BLAST jobs failed",
            "db": "https://stgworkload.blob.core.windows.net/blast-db/core_nt",
            "storage_account": "stgworkload",
        },
        include_database_metadata=True,
    )
    assert "exited with code 2" in out["error"]
    assert "exited with code 2" in out["output"]["error"]
    # The failed step (submit, no shard activity here) carries the same detail.
    assert "exited with code 2" in out["output"]["steps"]["submitting"]["error"]


def test_external_failed_enrichment_skipped_on_list_view(monkeypatch) -> None:
    # List rendering (include_database_metadata=False) must NOT pay for the
    # Storage read — the generic sibling message is left as-is.
    import api.services.blast.runtime_failure as runtime_failure

    def _boom(*_a, **_k):
        raise AssertionError("Storage read must not run on the list view")

    monkeypatch.setattr(runtime_failure, "read_blast_runtime_failure", _boom)
    out = _external_to_blast_job(
        {
            "job_id": "ext-2",
            "status": "failed",
            "error": "one or more BLAST jobs failed",
            "db": "https://stgworkload.blob.core.windows.net/blast-db/core_nt",
        },
    )
    assert out["error"] == "one or more BLAST jobs failed"


def test_external_failed_enrichment_preserves_specific_error(monkeypatch) -> None:
    # A genuinely specific sibling error is authoritative; do not overwrite it
    # even if a cluster-side detail happens to be readable.
    import api.services.blast.runtime_failure as runtime_failure

    monkeypatch.setattr(
        runtime_failure,
        "read_blast_runtime_failure",
        lambda account, job_id: "generic cluster blob detail",
    )
    out = _external_to_blast_job(
        {
            "job_id": "ext-3",
            "status": "failed",
            "error": {"code": "database_not_found", "message": "core_nt missing on node"},
            "db": "https://stgworkload.blob.core.windows.net/blast-db/core_nt",
        },
        include_database_metadata=True,
    )
    assert "core_nt missing on node" in out["error"]
    assert "generic cluster blob detail" not in out["error"]



