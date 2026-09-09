"""Tests for bounded, owner-scoped BLAST result comparison.

Responsibility: Verify pure hit-set comparison, truncation metadata, and HTTP authorization/
    readiness boundaries without real Storage or Service Bus operations.
Edit boundaries: Pure service and TestClient tests only.
Key entry points: ``test_*``.
Risky contracts: Both jobs require independent owner checks; partial reads must never be
    presented as complete and no result/state write may occur.
Validation: ``uv run pytest -q api/tests/test_blast_result_comparison.py``.
"""

from __future__ import annotations

from types import SimpleNamespace

from api.services.blast.result_comparison import ComparisonDataset, compare_datasets
from fastapi.testclient import TestClient

_OWNER = "00000000-0000-0000-0000-000000000000"


def _dataset(rows: dict[tuple[str, str], dict], *, truncated: bool = False) -> ComparisonDataset:
    return ComparisonDataset(
        hits=rows,
        total_hsps=sum(int(row.get("hsp_count") or 1) for row in rows.values()),
        files_seen=1,
        files_parsed=1,
        read_failures=0,
        truncated=truncated,
    )


def _hit(bitscore: float, *, identity: float = 99.0, hsp_count: int = 1) -> dict:
    return {
        "evalue": 1e-20,
        "bitscore": bitscore,
        "identity": identity,
        "query_cover": 100.0,
        "hsp_count": hsp_count,
        "title": "subject",
        "organism": "species",
    }


def test_compare_datasets_reports_added_removed_changed_and_partial() -> None:
    before = _dataset(
        {
            ("q1", "same"): _hit(100),
            ("q1", "changed"): _hit(80),
            ("q1", "removed"): _hit(70),
        }
    )
    after = _dataset(
        {
            ("q1", "same"): _hit(100),
            ("q1", "changed"): _hit(90),
            ("q2", "added"): _hit(60),
        },
        truncated=True,
    )

    result = compare_datasets(
        job_id="before",
        against_job_id="after",
        before=before,
        after=after,
        max_items=2,
    ).model_dump(mode="json")

    assert result["summary"] == {
        "before_hits": 3,
        "after_hits": 3,
        "common": 2,
        "unchanged": 1,
        "changed": 1,
        "added": 1,
        "removed": 1,
        "jaccard_percent": 50.0,
    }
    assert result["returned"] == 2
    assert result["truncated"] is True
    assert result["partial"] is True


class _Repo:
    def __init__(self, states: dict[str, SimpleNamespace]) -> None:
        self.states = states

    def get(self, job_id: str) -> SimpleNamespace | None:
        return self.states.get(job_id)


def _state(job_id: str, *, owner: str = _OWNER, status: str = "completed") -> SimpleNamespace:
    return SimpleNamespace(
        job_id=job_id,
        owner_oid=owner,
        status=status,
        storage_account="storage1",
        program="blastn",
        db="core_nt",
        payload={},
    )


def test_comparison_route_compares_two_owned_completed_jobs(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    states = {"before": _state("before"), "after": _state("after")}
    monkeypatch.setattr("api.services.state_repo.get_state_repo", lambda: _Repo(states))
    datasets = {
        "before": _dataset({("q", "a"): _hit(10)}),
        "after": _dataset({("q", "b"): _hit(20)}),
    }
    monkeypatch.setattr(
        "api.services.blast.result_comparison.load_comparison_dataset",
        lambda job_id, _storage: datasets[job_id],
    )

    from api.main import app

    response = TestClient(app).post(
        "/api/blast/jobs/before/comparison",
        json={"against_job_id": "after", "max_items": 50},
    )

    assert response.status_code == 200
    assert response.json()["summary"]["added"] == 1
    assert response.json()["summary"]["removed"] == 1


def test_comparison_route_rejects_same_nonterminal_and_foreign_jobs(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")

    from api.main import app

    client = TestClient(app)
    states = {"before": _state("before"), "running": _state("running", status="running")}
    monkeypatch.setattr("api.services.state_repo.get_state_repo", lambda: _Repo(states))

    same = client.post("/api/blast/jobs/before/comparison", json={"against_job_id": "before"})
    assert same.status_code == 422

    not_ready = client.post("/api/blast/jobs/before/comparison", json={"against_job_id": "running"})
    assert not_ready.status_code == 409
    assert not_ready.json()["code"] == "comparison_not_ready"

    states["foreign"] = _state("foreign", owner="another-user")
    foreign = client.post("/api/blast/jobs/before/comparison", json={"against_job_id": "foreign"})
    assert foreign.status_code == 403

    incompatible = _state("incompatible")
    incompatible.db = "nr"
    states["incompatible"] = incompatible
    mismatch = client.post(
        "/api/blast/jobs/before/comparison",
        json={"against_job_id": "incompatible"},
    )
    assert mismatch.status_code == 422
    assert mismatch.json()["code"] == "comparison_incompatible"
