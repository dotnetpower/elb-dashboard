"""Unit tests for `_local_to_blast_job` derived progress fields."""

from __future__ import annotations

from types import SimpleNamespace

from api.routes.stubs import _local_to_blast_job, _split_child_summaries_from_repo


def _state(**kw):
    base = dict(
        job_id="job-1",
        task_id="celery-1",
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
    assert out["status"] == "running"
    assert out["source"] == "dashboard"
    assert "splits_done" not in out  # no split children supplied


def test_local_to_blast_job_query_label_extracted():
    out = _local_to_blast_job(_state(payload={"query_file": "BRCA1.fa", "db": "16S_ribosomal_RNA"}))
    assert out["query_label"] == "BRCA1.fa"
    assert out["db"] == "16S_ribosomal_RNA"


def test_local_to_blast_job_exposes_error_for_frontend():
    out = _local_to_blast_job(_state(status="failed", phase="submit_failed", error_code="boom"))
    assert out["error_code"] == "boom"
    assert out["error"] == "boom"


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
