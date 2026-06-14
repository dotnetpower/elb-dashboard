"""Unit tests for `_local_to_blast_job` derived progress fields.

Responsibility: Unit tests for `_local_to_blast_job` derived progress fields
Edit boundaries: Keep assertions focused on the behavior under test; prefer fakes over live
Azure calls.
Key entry points: `_state`, `test_local_to_blast_job_minimum_shape`,
`test_local_to_blast_job_query_label_extracted`,
`test_local_to_blast_job_can_include_database_metadata`,
`test_local_to_blast_job_derives_storage_account_from_external_db_url`,
`test_local_to_blast_job_refuses_foreign_external_db_storage_account`,
`test_local_to_blast_job_exposes_error_for_frontend`,
`test_local_to_blast_job_exposes_progress_steps`,
`test_refresh_running_blast_state_skips_pre_runtime_phases`,
`test_refresh_running_blast_state_skips_without_runtime_job_id`
Risky contracts: Do not require network access or real Azure credentials unless the test is
explicitly integration-scoped.
Validation: `uv run pytest -q api/tests/test_local_to_blast_job.py`.
"""

from __future__ import annotations

from types import SimpleNamespace

from api.routes._blast_shared import _local_to_blast_job, _split_child_summaries_from_repo
from api.services.blast import job_state as blast_job_state


def _state(**kw):
    base = dict(
        job_id="job-1",
        task_id="celery-1",
        type="blast",
        status="running",
        phase="Running",
        created_at="2026-05-15T00:00:00Z",
        updated_at="2026-05-15T00:01:00Z",
        error_code=None,
        parent_job_id=None,
        payload={},
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_local_to_blast_job_minimum_shape():
    out = _local_to_blast_job(_state())
    assert out["job_id"] == "job-1"
    assert out["job_id_kind"] == "dashboard"
    assert out["dashboard_job_id"] == "job-1"
    assert out["target"]["links"]["dashboard_status"] == "/api/blast/jobs/job-1"
    assert out["status"] == "running"
    assert out["source"] == "dashboard"
    assert "splits_done" not in out  # no split children supplied


def test_local_to_blast_job_surfaces_owner_upn():
    # Recent searches User column relies on this contract — the JobState row
    # carries owner_upn alongside owner_oid so the UI can render a readable
    # submitter name without a Graph lookup.
    out_with_upn = _local_to_blast_job(_state(owner_upn="alice@example.com"))
    assert out_with_upn["owner_upn"] == "alice@example.com"

    out_without_upn = _local_to_blast_job(_state())
    assert out_without_upn["owner_upn"] is None


def test_local_to_blast_job_query_label_extracted():
    out = _local_to_blast_job(_state(payload={"query_file": "BRCA1.fa", "db": "16S_ribosomal_RNA"}))
    assert out["query_label"] == "BRCA1.fa"
    assert out["db"] == "16S_ribosomal_RNA"


def test_local_to_blast_job_can_include_database_metadata(monkeypatch):
    def fake_database_metadata(database: str, storage_account: str):
        assert database == "core_nt"
        assert storage_account == "elbstg01"
        return {"name": "core_nt", "title": "Core nucleotide BLAST database"}

    monkeypatch.setattr(blast_job_state, "_database_metadata_for_response", fake_database_metadata)

    out = _local_to_blast_job(
        _state(
            db="core_nt",
            storage_account="elbstg01",
            payload={"db": "core_nt", "storage_account": "elbstg01"},
        ),
        include_database_metadata=True,
    )

    assert out["database_metadata"] == {
        "name": "core_nt",
        "title": "Core nucleotide BLAST database",
    }


def test_local_to_blast_job_derives_storage_account_from_external_db_url(monkeypatch):
    # Jobs synced from the sibling OpenAPI leave infrastructure.storage_account
    # empty but carry the blob-URL database under payload.external.db. The
    # storage account MUST be recovered from that URL — but only when it matches
    # the deployment's configured workload account — so the Storage-backed
    # resolver can fill the sequence/letter counts and snapshot date.
    monkeypatch.delenv("AZURE_BLOB_ENDPOINT", raising=False)
    monkeypatch.delenv("AZURE_STORAGE_ACCOUNT", raising=False)
    monkeypatch.setenv("STORAGE_ACCOUNT_NAME", "stelbdashboard3abp67bppe")
    seen: dict[str, str] = {}

    def fake_database_metadata(database: str, storage_account: str):
        seen["database"] = database
        seen["storage_account"] = storage_account
        return {"name": "core_nt", "number_of_sequences": 125_940_211}

    monkeypatch.setattr(blast_job_state, "_database_metadata_for_response", fake_database_metadata)

    out = _local_to_blast_job(
        _state(
            db="core_nt",
            payload={
                "external": {
                    "db": (
                        "https://stelbdashboard3abp67bppe.blob.core.windows.net/"
                        "blast-db/core_nt/core_nt"
                    )
                }
            },
        ),
        include_database_metadata=True,
    )

    assert seen["database"] == "core_nt"
    assert seen["storage_account"] == "stelbdashboard3abp67bppe"
    assert out["database_metadata"]["number_of_sequences"] == 125_940_211


def test_local_to_blast_job_refuses_foreign_external_db_storage_account(monkeypatch):
    # SECURITY: an attacker-influenced external db URL pointing at a foreign
    # Storage account must NOT cause an authenticated Storage call (which would
    # leak the MI Storage token). The resolver is still invoked, but with an
    # empty storage account so it falls back to the static catalogue only.
    monkeypatch.delenv("AZURE_BLOB_ENDPOINT", raising=False)
    monkeypatch.delenv("AZURE_STORAGE_ACCOUNT", raising=False)
    monkeypatch.setenv("STORAGE_ACCOUNT_NAME", "stelbdashboard3abp67bppe")
    seen: dict[str, str] = {}

    def fake_database_metadata(database: str, storage_account: str):
        seen["storage_account"] = storage_account
        return None

    monkeypatch.setattr(blast_job_state, "_database_metadata_for_response", fake_database_metadata)

    _local_to_blast_job(
        _state(
            db="core_nt",
            payload={
                "external": {
                    "db": "https://attackeracct.blob.core.windows.net/blast-db/core_nt/core_nt"
                }
            },
        ),
        include_database_metadata=True,
    )

    assert seen["storage_account"] == ""


def test_local_to_blast_job_exposes_error_for_frontend():
    out = _local_to_blast_job(_state(status="failed", phase="submit_failed", error_code="boom"))
    assert out["error_code"] == "boom"
    assert out["error"] == "boom"


def test_local_to_blast_job_combines_machine_code_with_human_detail():
    # `_retry_or_fail` failures carry an opaque machine code on the row plus a
    # human-readable detail mirrored to payload.error. The projection surfaces
    # BOTH so the operator sees the classification AND the actual reason — the
    # raw code stays available under error_code for any frontend logic.
    out = _local_to_blast_job(
        _state(
            status="failed",
            phase="terminal_az_login_failed",
            error_code="terminal_az_login_failed",
            payload={"error": "az login --identity failed: ManagedIdentity unavailable"},
        )
    )
    assert out["error_code"] == "terminal_az_login_failed"
    assert out["error"] == (
        "terminal_az_login_failed: az login --identity failed: ManagedIdentity unavailable"
    )


def test_local_to_blast_job_surfaces_human_detail_when_no_machine_code():
    out = _local_to_blast_job(
        _state(status="failed", phase="failed", error_code=None, payload={"error": "boom detail"})
    )
    assert out["error"] == "boom detail"


def test_local_to_blast_job_external_origin_row_synthesizes_steps():
    # A synced `/v1/jobs` row embeds the sibling snapshot under
    # payload['external'] and carries no dashboard `_progress`. The projection
    # must still surface the honest step timeline (so the Execution Steps
    # section is not blank) driven off the row's LIVE status.
    out = _local_to_blast_job(
        _state(
            status="completed",
            phase="completed",
            payload={"external": {"job_id": "ext-1", "status": "running"}},
        )
    )
    steps = out["output"]["steps"]
    assert out["custom_status"]["steps"] == steps
    # Live row status (completed) wins over the stale embedded snapshot.
    assert steps["running"]["status"] == "completed"
    # Dashboard-only steps stay skipped, never faked.
    assert steps["warming_up"]["status"] == "skipped"
    assert steps["staging_db"]["status"] == "skipped"


def test_local_to_blast_job_external_failed_row_surfaces_error():
    out = _local_to_blast_job(
        _state(
            status="failed",
            phase="failed",
            payload={"external": {"job_id": "ext-2", "status": "failed"}},
        )
    )
    # Honest fallback error so the banner never shows "No detailed error".
    assert out["error"]
    assert "no error detail" in out["error"].lower()
    assert out["output"]["failed_step"] == "submitting"
    assert out["output"]["steps"]["submitting"]["status"] == "failed"


def test_local_to_blast_job_external_failed_row_uses_persisted_error_code():
    # When `_sync_external_jobs_to_table` recovered the real sibling failure
    # cause into the error_code column (the /v1/jobs LIST snapshot has no
    # `error` field), the external-origin projection MUST surface THAT cause —
    # in the banner AND the failed step's inline error — instead of the generic
    # "no error detail" placeholder the bare snapshot would yield.
    detail = (
        "BLAST database core_nt memory requirements exceed memory available "
        'on selected machine type "Standard_E16s_v5"'
    )
    out = _local_to_blast_job(
        _state(
            status="failed",
            phase="submit_failed",
            error_code=detail,
            payload={"external": {"job_id": "ext-mem", "status": "failed"}},
        )
    )
    assert out["error"] == detail
    assert "no error detail" not in (out["error"] or "").lower()
    failed_step = out["output"]["failed_step"]
    assert out["output"]["steps"][failed_step]["error"] == detail
    assert out["output"]["error"] == detail



def test_local_to_blast_job_external_failed_row_enriched_with_cluster_detail(monkeypatch):
    # On the detail view, a synced external failed row with only a generic/empty
    # sibling error recovers the authoritative cluster-side blastn detail from
    # the results container (keyed by the sibling openapi job id).
    monkeypatch.delenv("AZURE_BLOB_ENDPOINT", raising=False)
    monkeypatch.delenv("AZURE_STORAGE_ACCOUNT", raising=False)
    monkeypatch.setenv("STORAGE_ACCOUNT_NAME", "stelbdashboard3abp67bppe")
    monkeypatch.setattr(
        blast_job_state, "_database_metadata_for_response", lambda *_a, **_k: None
    )
    import api.services.blast.runtime_failure as runtime_failure

    seen: dict[str, str] = {}

    def fake_reader(account: str, job_id: str) -> str:
        seen["account"] = account
        seen["job_id"] = job_id
        return "BLAST search exited with code 2: bad alphabet"

    monkeypatch.setattr(runtime_failure, "read_blast_runtime_failure", fake_reader)

    out = _local_to_blast_job(
        _state(
            status="failed",
            phase="failed",
            payload={
                "external": {
                    "job_id": "ext-3",
                    "status": "failed",
                    "error": "one or more BLAST jobs failed",
                    "db": (
                        "https://stelbdashboard3abp67bppe.blob.core.windows.net/"
                        "blast-db/core_nt/core_nt"
                    ),
                }
            },
        ),
        include_database_metadata=True,
    )
    # Results are keyed by the sibling openapi job id, on the trusted account.
    assert seen["account"] == "stelbdashboard3abp67bppe"
    assert seen["job_id"] == "ext-3"
    assert "exited with code 2" in out["error"]
    assert "exited with code 2" in out["output"]["error"]
    assert "exited with code 2" in out["output"]["steps"]["submitting"]["error"]


def test_local_to_blast_job_external_enrichment_skipped_on_list_view(monkeypatch):
    # List rendering (include_database_metadata=False) must not pay for the
    # Storage read; the generic sibling error is left untouched.
    import api.services.blast.runtime_failure as runtime_failure

    def _boom(*_a, **_k):
        raise AssertionError("Storage read must not run on the list view")

    monkeypatch.setattr(runtime_failure, "read_blast_runtime_failure", _boom)
    out = _local_to_blast_job(
        _state(
            status="failed",
            phase="failed",
            payload={
                "external": {
                    "job_id": "ext-4",
                    "status": "failed",
                    "error": "one or more BLAST jobs failed",
                }
            },
        )
    )
    assert "one or more BLAST jobs failed" in out["output"]["error"]


def test_local_to_blast_job_does_not_expose_submit_slot_wait_as_error():
    out = _local_to_blast_job(
        _state(
            status="running",
            phase="waiting_for_submit_slot",
            error_code="blast_submit_lock_busy",
        )
    )
    assert out["error_code"] == "blast_submit_lock_busy"
    assert out["error"] == ""


def test_local_to_blast_job_suppresses_stale_error_on_completed_job():
    # A job transiently demoted to `worker_lost` and then reconciled to
    # `completed` once its results were detected keeps the stale top-level
    # error_code (the reconcile/finalize paths flip status+phase but do not
    # clear it). The user-facing `error` must be empty so the Run details
    # page does not paint a red `worker_lost` on a successful job; the raw
    # `error_code` is still surfaced for diagnostics.
    out = _local_to_blast_job(
        _state(status="completed", phase="completed", error_code="worker_lost")
    )
    assert out["error_code"] == "worker_lost"
    assert out["error"] == ""


def test_local_to_blast_job_exposes_progress_steps():
    out = _local_to_blast_job(
        _state(
            status="running",
            phase="submitting",
            payload={
                "_progress": {
                    "phase": "submitting",
                    "status": "running",
                    "steps": {
                        "submitting": {
                            "phase": "submitting",
                            "last_output": "kubectl logs...",
                        }
                    },
                }
            },
        )
    )
    assert out["custom_status"]["steps"]["submitting"]["last_output"] == "kubectl logs..."
    assert out["output"]["steps"]["submitting"]["phase"] == "submitting"


def test_local_to_blast_job_derives_splits_done_total():
    children = {
        "child_count": 6,
        "children_by_status": {
            "completed": 3,
            "running": 2,
            "failed": 1,
        },
        "children": [],
    }
    out = _local_to_blast_job(_state(), split_children=children)
    assert out["splits_total"] == 6
    assert out["splits_done"] == 3
    assert out["splits_failed"] == 1


def test_local_to_blast_job_handles_alt_completed_keys():
    children = {
        "child_count": 4,
        "children_by_status": {
            "Succeeded": 2,
            "SUCCESS": 1,
            "running": 1,
        },
        "children": [],
    }
    out = _local_to_blast_job(_state(), split_children=children)
    assert out["splits_done"] == 3  # case-insensitive + alt names
    assert out["splits_total"] == 4


def test_split_child_summaries_uses_owner_batch_query():
    child = _state(
        job_id="child-1",
        status="completed",
        phase="Completed",
        parent_job_id="parent-1",
        payload={"group_id": "g1", "query_file": "q1.fa"},
    )

    class Repo:
        def __init__(self) -> None:
            self.calls = 0

        def list_children_for_owner(self, owner_oid, parent_job_ids, *, limit):
            self.calls += 1
            assert owner_oid == "owner-1"
            assert parent_job_ids == ["parent-1", "parent-2"]
            assert limit == 5000
            return {"parent-1": [child], "parent-2": []}

        def list_children(self, *_args, **_kwargs):
            raise AssertionError("N+1 child lookup should not be used")

    repo = Repo()
    out = _split_child_summaries_from_repo(
        repo,
        "owner-1",
        ["parent-1", "parent-2"],
    )

    assert repo.calls == 1
    assert set(out) == {"parent-1"}
    assert out["parent-1"]["child_count"] == 1
    assert out["parent-1"]["children_by_status"] == {"completed": 1}


def test_read_blast_runtime_failure_prefers_stderr(monkeypatch):
    blobs = [
        {"name": "job-1/job-elastic/logs/BLAST_RUNTIME-000.out"},
        {"name": "job-1/job-elastic/metadata/FAILURE.txt"},
    ]
    texts = {
        "job-1/job-elastic/logs/BLAST_RUNTIME-000.out": (
            "1780 run start 000 blastn db 0.62 0.05 0.01 10%\n"
            "1780 run exitCode 000 2\n"
            "1780 run end 000\n"
        ),
        "job-1/job-elastic/metadata/FAILURE.txt": "BLAST engine error: bad option\n",
    }
    monkeypatch.setattr("api.services.get_credential", lambda: object())
    monkeypatch.setattr(
        "api.services.storage.blob_io.list_result_blobs",
        lambda *_a, **_k: blobs,
    )
    monkeypatch.setattr(
        "api.services.storage.blob_io.read_blob_text",
        lambda _c, _a, _ct, path, **_k: texts[path],
    )

    detail = blast_job_state._read_blast_runtime_failure("stelb", "job-1")

    assert detail == "BLAST search exited with code 2: BLAST engine error: bad option"


def test_read_blast_runtime_failure_redacts_secrets_in_stderr(monkeypatch):
    # FAILURE.txt is runner-captured stderr that can embed a SAS token / Bearer
    # / subscription GUID (e.g. from an azcopy diagnostic). The reader MUST
    # redact at the source so BOTH the K8s-refresh step error and the external
    # projection surface secret-free text (Charter §12).
    blobs = [
        {"name": "job-1/job-elastic/logs/BLAST_RUNTIME-000.out"},
        {"name": "job-1/job-elastic/metadata/FAILURE.txt"},
    ]
    texts = {
        "job-1/job-elastic/logs/BLAST_RUNTIME-000.out": "1780 run exitCode 000 2\n",
        "job-1/job-elastic/metadata/FAILURE.txt": (
            "azcopy failed: https://acct.blob.core.windows.net/c/b"
            "?sv=2021&sig=AAAABBBBCCCCsecretsig%3D"
        ),
    }
    monkeypatch.setattr("api.services.get_credential", lambda: object())
    monkeypatch.setattr(
        "api.services.storage.blob_io.list_result_blobs",
        lambda *_a, **_k: blobs,
    )
    monkeypatch.setattr(
        "api.services.storage.blob_io.read_blob_text",
        lambda _c, _a, _ct, path, **_k: texts[path],
    )

    detail = blast_job_state._read_blast_runtime_failure("stelb", "job-1")

    assert "sig=AAAABBBBCCCC" not in detail
    assert "secretsig" not in detail
    assert "exited with code 2" in detail


def test_read_blast_runtime_failure_generic_when_no_stderr(monkeypatch):
    blobs = [{"name": "job-1/job-elastic/logs/BLAST_RUNTIME-000.out"}]
    monkeypatch.setattr("api.services.get_credential", lambda: object())
    monkeypatch.setattr(
        "api.services.storage.blob_io.list_result_blobs",
        lambda *_a, **_k: blobs,
    )
    monkeypatch.setattr(
        "api.services.storage.blob_io.read_blob_text",
        lambda *_a, **_k: "1780 run exitCode 000 2\n",
    )

    detail = blast_job_state._read_blast_runtime_failure("stelb", "job-1")

    assert "exited with code 2" in detail
    assert "not being staged on the assigned node" in detail


def test_read_blast_runtime_failure_empty_when_unreadable(monkeypatch):
    monkeypatch.setattr("api.services.get_credential", lambda: object())

    def _raise(*_a, **_k):
        raise RuntimeError("network blocked")

    monkeypatch.setattr("api.services.storage.blob_io.list_result_blobs", _raise)

    assert blast_job_state._read_blast_runtime_failure("stelb", "job-1") == ""
    assert blast_job_state._read_blast_runtime_failure("", "job-1") == ""


def test_refresh_running_blast_state_waits_for_result_artifacts(monkeypatch):
    state = _state(
        status="running",
        phase="submitted",
        payload={
            "subscription_id": "sub-1",
            "resource_group": "rg-elb",
            "cluster_name": "elb-cluster",
            "storage_account": "stelb",
            "elastic_blast_job_id": "job-elastic",
        },
    )

    class Repo:
        def __init__(self) -> None:
            self.updated = None
            self.history = []

        def update(self, job_id, **kwargs):
            self.updated = (job_id, kwargs)
            return _state(**{**state.__dict__, **kwargs})

        def append_history(self, job_id, event, payload):
            self.history.append((job_id, event, payload))

    repo = Repo()
    monkeypatch.setattr("api.services.get_credential", lambda: object())
    monkeypatch.setattr(
        "api.services.monitoring.k8s_check_blast_status",
        lambda *_args, **_kwargs: {"status": "completed"},
    )
    monkeypatch.setattr(blast_job_state, "_state_has_parseable_result_artifact", lambda *_: False)

    refreshed = blast_job_state._refresh_running_blast_state(repo, state)

    assert refreshed.status == "running"
    assert refreshed.phase == "results_pending"
    assert repo.updated[0] == "job-1"
    assert repo.updated[1]["status"] == "running"
    assert repo.updated[1]["phase"] == "results_pending"
    progress = repo.updated[1]["payload"]["_progress"]
    assert progress["phase"] == "results_pending"
    assert progress["steps"]["exporting_results"]["phase"] == "results_pending"
    assert repo.history[0][1] == "k8s_completed_results_pending"


def test_refresh_running_blast_state_completes_prior_running_steps(monkeypatch):
    state = _state(
        status="running",
        phase="submitted",
        payload={
            "subscription_id": "sub-1",
            "resource_group": "rg-elb",
            "cluster_name": "elb-cluster",
            "storage_account": "stelb",
            "elastic_blast_job_id": "job-elastic",
            "_progress": {
                "phase": "submitting",
                "status": "running",
                "steps": {
                    "submitting": {
                        "phase": "submitting",
                        "status": "running",
                        "last_output": "elastic-blast submit log",
                    }
                },
            },
        },
    )

    class Repo:
        def __init__(self) -> None:
            self.updated = None
            self.history = []

        def update(self, job_id, **kwargs):
            self.updated = (job_id, kwargs)
            return _state(**{**state.__dict__, **kwargs})

        def append_history(self, job_id, event, payload):
            self.history.append((job_id, event, payload))

    repo = Repo()
    monkeypatch.setattr("api.services.get_credential", lambda: object())
    monkeypatch.setattr(
        "api.services.monitoring.k8s_check_blast_status",
        lambda *_args, **_kwargs: {"status": "completed", "job_id": "job-elastic"},
    )
    monkeypatch.setattr(blast_job_state, "_state_has_parseable_result_artifact", lambda *_: True)

    refreshed = blast_job_state._refresh_running_blast_state(repo, state)

    assert refreshed.status == "completed"
    assert refreshed.phase == "completed"
    progress = repo.updated[1]["payload"]["_progress"]
    assert progress["phase"] == "completed"
    assert progress["steps"]["submitting"]["status"] == "completed"
    assert progress["steps"]["submitting"]["success"] is True
    assert progress["steps"]["submitting"]["last_output"] == "elastic-blast submit log"
    assert progress["steps"]["completed"]["status"] == "completed"
    assert progress["steps"]["completed"]["success"] is True


def test_refresh_running_blast_state_failure_marks_running_step_with_detail(monkeypatch):
    # A K8s-stage search failure (after submit succeeded) must be recorded
    # against the real execution step ("running") with the cluster-side blastn
    # detail — not the bare "failed" phase that the SPA mislabels as
    # "Submit Job" while showing a benign helper log line.
    state = _state(
        status="running",
        phase="submitted",
        payload={
            "subscription_id": "sub-1",
            "resource_group": "rg-elb",
            "cluster_name": "elb-cluster",
            "storage_account": "stelb",
            "elastic_blast_job_id": "job-elastic",
            "_progress": {
                "phase": "submitting",
                "status": "running",
                "steps": {
                    "submitting": {
                        "phase": "submitting",
                        "status": "running",
                        "last_output": "[parallel-prep] running 4 azcopy checks concurrently",
                    }
                },
            },
        },
    )

    class Repo:
        def __init__(self) -> None:
            self.updated = None
            self.history = []

        def update(self, job_id, **kwargs):
            self.updated = (job_id, kwargs)
            return _state(**{**state.__dict__, **kwargs})

        def append_history(self, job_id, event, payload):
            self.history.append((job_id, event, payload))

    repo = Repo()
    monkeypatch.setattr("api.services.get_credential", lambda: object())
    monkeypatch.setattr(
        "api.services.monitoring.k8s_check_blast_status",
        lambda *_args, **_kwargs: {"status": "failed", "job_id": "job-elastic", "failed": 1},
    )
    monkeypatch.setattr(
        blast_job_state,
        "_read_blast_runtime_failure",
        lambda *_args, **_kwargs: "BLAST search exited with code 2: bad option",
    )

    refreshed = blast_job_state._refresh_running_blast_state(repo, state)

    assert refreshed.status == "failed"
    assert refreshed.phase == "failed"
    assert refreshed.error_code == "blast_search_failed"
    payload = repo.updated[1]["payload"]
    steps = payload["_progress"]["steps"]
    # The running step carries the failure, and prior steps are completed.
    assert steps["running"]["status"] == "failed"
    assert steps["running"]["success"] is False
    assert steps["running"]["error"] == "BLAST search exited with code 2: bad option"
    assert steps["submitting"]["status"] == "completed"
    assert steps["submitting"]["success"] is True
    # Top-level hints for the SPA banner.
    assert payload["failed_step"] == "running"
    assert payload["error"] == "BLAST search exited with code 2: bad option"
    # The serialized job exposes the hints to the frontend.
    out = _local_to_blast_job(refreshed)
    assert out["output"]["failed_step"] == "running"
    assert out["output"]["error"] == "BLAST search exited with code 2: bad option"


def test_refresh_running_blast_state_failure_falls_back_to_generic_detail(monkeypatch):
    # When the runner captured no stderr / runtime artifact, the failure still
    # reports a concise generic message instead of an empty banner.
    state = _state(
        status="running",
        phase="running",
        payload={
            "subscription_id": "sub-1",
            "resource_group": "rg-elb",
            "cluster_name": "elb-cluster",
            "storage_account": "stelb",
            "elastic_blast_job_id": "job-elastic",
            "_progress": {"phase": "running", "status": "running", "steps": {}},
        },
    )

    class Repo:
        def __init__(self) -> None:
            self.updated = None

        def update(self, job_id, **kwargs):
            self.updated = (job_id, kwargs)
            return _state(**{**state.__dict__, **kwargs})

        def append_history(self, *_args, **_kwargs):
            pass

    repo = Repo()
    monkeypatch.setattr("api.services.get_credential", lambda: object())
    monkeypatch.setattr(
        "api.services.monitoring.k8s_check_blast_status",
        lambda *_args, **_kwargs: {"status": "failed", "job_id": "job-elastic", "failed": 2},
    )
    monkeypatch.setattr(
        blast_job_state, "_read_blast_runtime_failure", lambda *_args, **_kwargs: ""
    )

    refreshed = blast_job_state._refresh_running_blast_state(repo, state)

    assert refreshed.status == "failed"
    steps = repo.updated[1]["payload"]["_progress"]["steps"]
    assert steps["running"]["status"] == "failed"
    assert "2 pod(s) failed" in steps["running"]["error"]


def test_refresh_running_blast_state_uses_discovered_elastic_blast_job_id(monkeypatch):
    state = _state(
        status="running",
        phase="submitted",
        payload={
            "subscription_id": "sub-1",
            "resource_group": "rg-elb",
            "cluster_name": "elb-cluster",
            "storage_account": "stelb",
        },
    )

    class Repo:
        def update(self, job_id, **kwargs):
            return _state(**{**state.__dict__, **kwargs})

        def append_history(self, *_args, **_kwargs):
            pass

    seen = {}

    def fake_k8s(*_args, **kwargs):
        seen["job_id"] = kwargs.get("job_id")
        return {"status": "running", "job_id": kwargs.get("job_id")}

    monkeypatch.setattr("api.services.get_credential", lambda: object())
    monkeypatch.setattr(blast_job_state, "_discover_elastic_blast_job_id", lambda *_: "job-elastic")
    monkeypatch.setattr("api.services.monitoring.k8s_check_blast_status", fake_k8s)

    refreshed = blast_job_state._refresh_running_blast_state(Repo(), state)

    assert refreshed is state
    assert seen["job_id"] == "job-elastic"


def test_refresh_running_blast_state_throttles_repeated_k8s_checks(monkeypatch):
    state = _state(
        job_id="job-throttle",
        status="running",
        phase="submitted",
        payload={
            "subscription_id": "sub-1",
            "resource_group": "rg-elb",
            "cluster_name": "elb-cluster",
            "storage_account": "stelb",
            "elastic_blast_job_id": "job-elastic",
        },
    )
    calls = 0
    times = iter([100.0, 105.0])

    def fake_k8s(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return {"status": "running", "job_id": "job-elastic"}

    blast_job_state._K8S_REFRESH_LAST_CHECK.clear()
    monkeypatch.setattr(blast_job_state, "monotonic", lambda: next(times))
    monkeypatch.setattr("api.services.get_credential", lambda: object())
    monkeypatch.setattr("api.services.monitoring.k8s_check_blast_status", fake_k8s)

    first = blast_job_state._refresh_running_blast_state(object(), state)
    second = blast_job_state._refresh_running_blast_state(object(), state)

    assert first is state
    assert second is state
    assert calls == 1


def test_refresh_running_blast_state_skips_pre_runtime_phases(monkeypatch):
    state = _state(
        status="running",
        phase="staging_db",
        payload={
            "subscription_id": "sub-1",
            "resource_group": "rg-elb",
            "cluster_name": "elb-cluster",
            "storage_account": "stelb",
        },
    )
    called = False

    def fake_k8s(*_args, **_kwargs):
        nonlocal called
        called = True
        return {"status": "running"}

    monkeypatch.setattr("api.services.monitoring.k8s_check_blast_status", fake_k8s)

    refreshed = blast_job_state._refresh_running_blast_state(object(), state)

    assert refreshed is state
    assert called is False


def test_refresh_running_blast_state_skips_without_runtime_job_id(monkeypatch):
    state = _state(
        status="running",
        phase="submitted",
        payload={
            "subscription_id": "sub-1",
            "resource_group": "rg-elb",
            "cluster_name": "elb-cluster",
            "storage_account": "stelb",
        },
    )
    called = False

    def fake_k8s(*_args, **_kwargs):
        nonlocal called
        called = True
        return {"status": "running"}

    monkeypatch.setattr(blast_job_state, "_discover_elastic_blast_job_id", lambda *_: "")
    monkeypatch.setattr("api.services.monitoring.k8s_check_blast_status", fake_k8s)

    refreshed = blast_job_state._refresh_running_blast_state(object(), state)

    assert refreshed is state
    assert called is False


def test_refresh_running_blast_state_running_phase_uses_short_throttle(monkeypatch):
    """`running` and `results_pending` use the 5 s throttle, not 20 s."""
    state = _state(
        job_id="job-running-throttle",
        status="running",
        phase="running",
        payload={
            "subscription_id": "sub-1",
            "resource_group": "rg-elb",
            "cluster_name": "elb-cluster",
            "storage_account": "stelb",
            "elastic_blast_job_id": "job-elastic",
        },
    )
    calls = 0
    # First call at t=100 → k8s_check_blast_status fires once.
    # Second call at t=106 → 6 s > 5 s short floor → k8s fires again.
    # Third call at t=109 → 3 s < 5 s → throttled.
    times = iter([100.0, 106.0, 109.0])

    def fake_k8s(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return {"status": "running", "job_id": "job-elastic"}

    blast_job_state._K8S_REFRESH_LAST_CHECK.clear()
    monkeypatch.setattr(blast_job_state, "monotonic", lambda: next(times))
    monkeypatch.setattr("api.services.get_credential", lambda: object())
    monkeypatch.setattr("api.services.monitoring.k8s_check_blast_status", fake_k8s)

    blast_job_state._refresh_running_blast_state(object(), state)
    blast_job_state._refresh_running_blast_state(object(), state)
    blast_job_state._refresh_running_blast_state(object(), state)

    assert calls == 2


def test_refresh_running_blast_state_reads_top_level_columns(monkeypatch):
    """Refresh works when payload was omitted (list endpoint path).

    `list_for_owner(..., include_payload=False)` returns rows without
    `payload`. The function must read scope from top-level columns
    (`state.subscription_id`, etc.) and reload the full payload only
    when it actually needs to mutate the row.
    """
    state = _state(
        job_id="job-list",
        status="running",
        phase="running",
        payload={},  # empty — simulates include_payload=False
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="elb-cluster",
        storage_account="stelb",
    )
    full_state = _state(
        job_id="job-list",
        status="running",
        phase="running",
        payload={
            "_progress": {
                "phase": "running",
                "status": "running",
                "steps": {"running": {"phase": "running", "started_at": "2026-05-21T00:00:00Z"}},
            },
            "elastic_blast_job_id": "job-elastic",
        },
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="elb-cluster",
        storage_account="stelb",
    )

    class Repo:
        def __init__(self) -> None:
            self.updated = None
            self.history = []

        def get(self, job_id):
            assert job_id == "job-list"
            return full_state

        def update(self, job_id, **kwargs):
            self.updated = (job_id, kwargs)
            return _state(**{**full_state.__dict__, **kwargs})

        def append_history(self, *args, **_kwargs):
            self.history.append(args)

    repo = Repo()
    blast_job_state._K8S_REFRESH_LAST_CHECK.clear()
    monkeypatch.setattr(blast_job_state, "_discover_elastic_blast_job_id", lambda *_: "job-elastic")
    monkeypatch.setattr("api.services.get_credential", lambda: object())
    monkeypatch.setattr(
        "api.services.monitoring.k8s_check_blast_status",
        lambda *_args, **_kwargs: {"status": "completed", "job_id": "job-elastic"},
    )
    monkeypatch.setattr(blast_job_state, "_state_has_parseable_result_artifact", lambda *_: True)

    refreshed = blast_job_state._refresh_running_blast_state(repo, state)

    # Reloaded full payload existed and was used to merge progress.
    assert repo.updated is not None
    assert repo.updated[0] == "job-list"
    progress = repo.updated[1]["payload"]["_progress"]
    # The existing running step from the reloaded payload must survive
    # the merge — otherwise the list endpoint would erase step history.
    assert "started_at" in progress["steps"]["running"]
    assert refreshed.status == "completed"


def test_local_to_blast_job_marks_stale_when_refresh_blocked():
    """A running row whose cluster is stopped is tagged stale, not silently
    left looking like it is still in progress."""
    out = _local_to_blast_job(
        _state(status="running", phase="running"),
        refresh_blocked_reason="cluster_stopped",
        cluster_power_state="Stopped",
    )
    assert out["stale"] is True
    assert out["refresh_blocked_reason"] == "cluster_stopped"
    assert out["cluster_power_state"] == "Stopped"


def test_local_to_blast_job_no_stale_for_terminal_rows():
    """Completed/failed rows are never marked stale even if the cluster is
    down — their last-known status is final, not frozen."""
    out = _local_to_blast_job(
        _state(status="completed", phase="completed"),
        refresh_blocked_reason="cluster_stopped",
        cluster_power_state="Stopped",
    )
    assert "stale" not in out
    assert "refresh_blocked_reason" not in out


def test_local_to_blast_job_stale_omits_power_state_when_unknown():
    out = _local_to_blast_job(
        _state(status="running", phase="submitted"),
        refresh_blocked_reason="cluster_not_found",
        cluster_power_state=None,
    )
    assert out["stale"] is True
    assert out["refresh_blocked_reason"] == "cluster_not_found"
    assert "cluster_power_state" not in out


def test_blocked_refresh_reasons_flags_stopped_cluster(monkeypatch):
    """`_blocked_refresh_reasons` returns the active rows whose cluster is not
    Running, costing one cached ARM probe per distinct scope."""
    rows = [
        _state(
            job_id="job-a",
            status="running",
            phase="running",
            subscription_id="sub-1",
            resource_group="rg-elb",
            cluster_name="elb-cluster",
        ),
        _state(
            job_id="job-b",
            status="running",
            phase="submitted",
            subscription_id="sub-1",
            resource_group="rg-elb",
            cluster_name="elb-cluster",
        ),
        # Terminal row — never gated.
        _state(
            job_id="job-done",
            status="completed",
            phase="completed",
            subscription_id="sub-1",
            resource_group="rg-elb",
            cluster_name="elb-cluster",
        ),
    ]
    probe_calls: list[tuple[str, str, str]] = []

    def fake_health(_cred, sub, rg, cluster):
        probe_calls.append((sub, rg, cluster))
        return {
            "healthy": False,
            "exists": True,
            "power_state": "Stopped",
            "reason": "cluster_stopped",
        }

    monkeypatch.setattr("api.services.get_credential", lambda: object())
    monkeypatch.setattr("api.services.cluster_health.get_cluster_health", fake_health)

    blocked = blast_job_state._blocked_refresh_reasons(rows)

    assert set(blocked) == {"job-a", "job-b"}
    assert blocked["job-a"]["reason"] == "cluster_stopped"
    assert blocked["job-a"]["power_state"] == "Stopped"
    # One ARM probe for the single shared (sub, rg, cluster) scope.
    assert probe_calls == [("sub-1", "rg-elb", "elb-cluster")]


def test_blocked_refresh_reasons_empty_when_cluster_running(monkeypatch):
    rows = [
        _state(
            job_id="job-a",
            status="running",
            phase="running",
            subscription_id="sub-1",
            resource_group="rg-elb",
            cluster_name="elb-cluster",
        ),
    ]
    monkeypatch.setattr("api.services.get_credential", lambda: object())
    monkeypatch.setattr(
        "api.services.cluster_health.get_cluster_health",
        lambda *_a, **_kw: {
            "healthy": True,
            "exists": True,
            "power_state": "Running",
            "reason": None,
        },
    )

    assert blast_job_state._blocked_refresh_reasons(rows) == {}


def test_blocked_refresh_reasons_skips_when_no_active_rows(monkeypatch):
    """No active rows → no credential fetch, no ARM probe."""

    def boom():
        raise AssertionError("get_credential must not be called without active rows")

    monkeypatch.setattr("api.services.get_credential", boom)
    rows = [_state(job_id="job-done", status="completed", phase="completed")]
    assert blast_job_state._blocked_refresh_reasons(rows) == {}

