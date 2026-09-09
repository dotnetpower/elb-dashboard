"""Tests for read-only split-query child detail projection and routing.

Responsibility: Verify shard aggregation, sanitization, deterministic ordering, ownership,
    and empty-parent behavior without Azure, Kubernetes, or Service Bus calls.
Edit boundaries: Pure service and TestClient tests only.
Key entry points: ``test_*``.
Risky contracts: A parent authorization must never expose a malformed child owned by another
    caller, and child payloads or Storage paths must never be returned wholesale.
Validation: ``uv run pytest -q api/tests/test_blast_shard_details.py``.
"""

from __future__ import annotations

from types import SimpleNamespace

from api.services.blast.split_details import build_split_details
from fastapi.testclient import TestClient

_OWNER = "00000000-0000-0000-0000-000000000000"


def _child(
    job_id: str,
    *,
    status: str,
    group_id: str,
    owner_oid: str = _OWNER,
    error: str = "",
) -> SimpleNamespace:
    return SimpleNamespace(
        job_id=job_id,
        owner_oid=owner_oid,
        status=status,
        phase=status,
        error_code="child_failed" if error else "",
        created_at="2026-09-01T00:00:00+00:00",
        updated_at="2026-09-01T00:01:30+00:00",
        payload={
            "group_id": group_id,
            "query_file": f"queries/private/{group_id}.fa",
            "effective_search_space": "1234",
            "error": error,
            "secret": "must-not-leak",
        },
    )


def test_build_split_details_counts_sorts_and_sanitizes() -> None:
    response = build_split_details(
        "parent-1",
        [
            _child("child-b", status="running", group_id="group-b"),
            _child(
                "child-a",
                status="failed",
                group_id="group-a",
                error="failed in subscription 11111111-1111-1111-1111-111111111111",
            ),
        ],
    ).model_dump(mode="json")

    assert response["summary"] == {
        "total": 2,
        "completed": 0,
        "failed": 1,
        "cancelled": 0,
        "active": 1,
        "other": 0,
        "terminal": 1,
        "progress_percent": 50.0,
    }
    assert [shard["job_id"] for shard in response["shards"]] == ["child-a", "child-b"]
    assert response["shards"][0]["query_file"] == "group-a.fa"
    assert response["shards"][0]["duration_seconds"] == 90.0
    assert "11111111-1111-1111-1111-111111111111" not in response["shards"][0]["error"]
    assert "secret" not in response["shards"][0]


def test_build_split_details_bounds_corrupt_values() -> None:
    child = _child("x" * 200, status="running", group_id="group")
    child.phase = "Bearer private-token-value-1234567890"
    child.error_code = "code-" + "x" * 200
    child.payload["query_file"] = (
        "https://acct.blob.core.windows.net/queries/private.fa?sv=1&sp=r&sig=secret"
    )
    child.payload["effective_search_space"] = 10**1000
    child.updated_at = "2026-09-01T00:01:30"

    shard = build_split_details("parent", [child]).shards[0]

    assert len(shard.job_id) == 128
    assert "private-token" not in shard.phase
    assert len(shard.error_code or "") == 80
    assert shard.query_file == "private.fa"
    assert shard.effective_search_space is None
    assert shard.duration_seconds is None


class _Repo:
    def __init__(self, parent: SimpleNamespace | None, children: list[SimpleNamespace]) -> None:
        self.parent = parent
        self.children = children

    def get(self, job_id: str) -> SimpleNamespace | None:
        return self.parent if job_id != "missing" else None

    def list_children(self, parent_job_id: str, limit: int = 1000) -> list[SimpleNamespace]:
        assert parent_job_id == "parent-1"
        assert limit == 1001
        return self.children[:limit]


def _parent(owner_oid: str = _OWNER) -> SimpleNamespace:
    return SimpleNamespace(job_id="parent-1", owner_oid=owner_oid)


def test_shard_route_returns_empty_and_filters_foreign_child(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    children = [
        _child("visible", status="completed", group_id="g1"),
        _child("hidden", status="completed", group_id="g2", owner_oid="other"),
    ]
    monkeypatch.setattr(
        "api.services.state_repo.get_state_repo",
        lambda: _Repo(_parent(), children),
    )

    from api.main import app

    response = TestClient(app).get("/api/blast/jobs/parent-1/shards")

    assert response.status_code == 200
    assert response.json()["summary"]["total"] == 1
    assert [row["job_id"] for row in response.json()["shards"]] == ["visible"]


def test_shard_route_enforces_parent_owner_and_missing(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")

    from api.main import app

    client = TestClient(app)
    monkeypatch.setattr(
        "api.services.state_repo.get_state_repo",
        lambda: _Repo(_parent("other"), []),
    )
    assert client.get("/api/blast/jobs/parent-1/shards").status_code == 403

    monkeypatch.setattr(
        "api.services.state_repo.get_state_repo",
        lambda: _Repo(None, []),
    )
    assert client.get("/api/blast/jobs/missing/shards").status_code == 404


def test_shard_route_reports_truncation(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    children = [
        _child(f"child-{index}", status="completed", group_id=f"g-{index:04d}")
        for index in range(1001)
    ]
    monkeypatch.setattr(
        "api.services.state_repo.get_state_repo",
        lambda: _Repo(_parent(), children),
    )

    from api.main import app

    response = TestClient(app).get("/api/blast/jobs/parent-1/shards")

    assert response.status_code == 200
    assert response.json()["truncated"] is True
    assert len(response.json()["shards"]) == 1000
