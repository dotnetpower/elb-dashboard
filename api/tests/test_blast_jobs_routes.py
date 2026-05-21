"""HTTP-level tests for BLAST job detail routes.

Responsibility: HTTP-level tests for BLAST job list/detail response shaping
Edit boundaries: Keep tests focused on route behavior; use fakes instead of live Azure calls.
Key entry points: `test_job_detail_skips_split_child_lookup_for_non_split_job`
Risky contracts: Do not require network access or real Azure credentials unless the test is
explicitly integration-scoped.
Validation: `uv run pytest -q api/tests/test_blast_jobs_routes.py`.
"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient


def test_job_detail_skips_split_child_lookup_for_non_split_job(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")

    owner_oid = "00000000-0000-0000-0000-000000000000"

    class Repo:
        def get(self, job_id: str):
            assert job_id == "job-1"
            return SimpleNamespace(
                job_id="job-1",
                task_id="task-1",
                type="blast",
                owner_oid=owner_oid,
                status="completed",
                phase="completed",
                created_at="2026-05-21T00:00:00Z",
                updated_at="2026-05-21T00:01:00Z",
                error_code=None,
                parent_job_id=None,
                payload={"db": "core_nt", "query_file": "query.fa"},
            )

        def list_children(self, *_args, **_kwargs):
            raise AssertionError("non-split job detail should not query child rows")

    monkeypatch.setattr("api.services.state_repo.JobStateRepository", Repo)

    from api.main import app

    client = TestClient(app)
    response = client.get(
        "/api/blast/jobs/job-1",
        params={"include_database_metadata": "false"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["job_id"] == "job-1"
    assert body["status"] == "completed"
    assert "split_children" not in body
    assert "database_metadata" not in body
