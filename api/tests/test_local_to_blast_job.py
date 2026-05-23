"""Unit tests for `_local_to_blast_job` derived progress fields.

Responsibility: Unit tests for `_local_to_blast_job` derived progress fields
Edit boundaries: Keep assertions focused on the behavior under test; prefer fakes over live
Azure calls.
Key entry points: `_state`, `test_local_to_blast_job_minimum_shape`,
`test_local_to_blast_job_query_label_extracted`,
`test_local_to_blast_job_can_include_database_metadata`,
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


def test_local_to_blast_job_exposes_error_for_frontend():
    out = _local_to_blast_job(_state(status="failed", phase="submit_failed", error_code="boom"))
    assert out["error_code"] == "boom"
    assert out["error"] == "boom"


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

