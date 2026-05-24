"""Tests for `/api/aks/recent-failed-provisions`.

Responsibility: Verify the route filters by `type="aks_provision"`,
    `status="failed"`, the caller's `owner_oid`, and the freshness
    window. Locks in the response shape the FE banner depends on.
Edit boundaries: Pure unit tests with a stubbed JobStateRepository.
Key entry points: see per-test docstrings.
Risky contracts: When the JobState dataclass adds/removes fields the
    response shape assertions act as the canary.
Validation: `uv run pytest -q api/tests/test_aks_recent_failed_provisions.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setenv("API_CLIENT_ID", "00000000-0000-0000-0000-000000000000")
    from api.main import app

    return TestClient(app)


def _make_state(
    *,
    job_id: str,
    job_type: str,
    status: str,
    hours_ago: float,
    cluster_name: str = "elb-cluster-01",
    region: str = "koreacentral",
    resource_group: str = "rg-elb-cluster",
    subscription_id: str = "sub-1",
    task_id: str = "task-abc",
    error_code: str = "ErrCode_InsufficientVCPUQuota",
    phase: str = "arm_create_or_update",
) -> Any:
    """Build a duck-typed JobState close enough for the route handler."""
    from api.services.state.job_state import JobState

    when = (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat(
        timespec="seconds"
    )
    return JobState(
        job_id=job_id,
        type=job_type,
        status=status,
        phase=phase,
        owner_oid="anonymous",  # AUTH_DEV_BYPASS synthesises this
        task_id=task_id,
        error_code=error_code,
        created_at=when,
        updated_at=when,
        cluster_name=cluster_name,
        resource_group=resource_group,
        subscription_id=subscription_id,
        payload={
            "region": region,
            "cluster_name": cluster_name,
            "resource_group": resource_group,
            "subscription_id": subscription_id,
        },
    )


def _patch_repo(monkeypatch: pytest.MonkeyPatch, rows: list[Any]) -> None:
    import api.routes.aks.recent_failures as route_mod

    class _FakeRepo:
        def list_for_owner(
            self, _oid: str, *, limit: int = 0, include_payload: bool = False
        ) -> list[Any]:
            return list(rows)

    monkeypatch.setattr(route_mod, "get_state_repo", lambda: _FakeRepo())


def test_returns_only_recent_aks_failures(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The route filters to type=aks_provision + status=failed + within
    the freshness window. Other job types and stale rows are dropped."""
    rows = [
        _make_state(job_id="j-1", job_type="aks_provision", status="failed", hours_ago=1),
        _make_state(job_id="j-2", job_type="blast", status="failed", hours_ago=1),
        _make_state(
            job_id="j-3",
            job_type="aks_provision",
            status="failed",
            hours_ago=72,  # stale
        ),
        _make_state(
            job_id="j-4", job_type="aks_provision", status="completed", hours_ago=1
        ),
    ]
    _patch_repo(monkeypatch, rows)
    resp = client.get("/api/aks/recent-failed-provisions?hours=24")
    assert resp.status_code == 200
    body = resp.json()
    assert body["degraded"] is False
    jobs = body["jobs"]
    assert len(jobs) == 1
    assert jobs[0]["job_id"] == "j-1"
    assert jobs[0]["task_id"] == "task-abc"
    assert jobs[0]["region"] == "koreacentral"
    assert jobs[0]["error_code"] == "ErrCode_InsufficientVCPUQuota"


def test_orders_jobs_newest_first(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Multiple recent failures are returned newest-first so the
    dashboard banner shows the most relevant one."""
    rows = [
        _make_state(job_id="old", job_type="aks_provision", status="failed", hours_ago=5),
        _make_state(job_id="new", job_type="aks_provision", status="failed", hours_ago=1),
    ]
    _patch_repo(monkeypatch, rows)
    resp = client.get("/api/aks/recent-failed-provisions?hours=24")
    assert resp.status_code == 200
    job_ids = [j["job_id"] for j in resp.json()["jobs"]]
    assert job_ids == ["new", "old"]


def test_returns_degraded_payload_on_repo_failure(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exception inside `list_for_owner` (table outage, credential
    blip, etc.) must surface as `degraded=true` with an empty list
    rather than 500."""
    import api.routes.aks.recent_failures as route_mod

    class _BrokenRepo:
        def list_for_owner(self, *_a: Any, **_kw: Any) -> list[Any]:
            raise RuntimeError("table unavailable")

    monkeypatch.setattr(route_mod, "get_state_repo", lambda: _BrokenRepo())
    resp = client.get("/api/aks/recent-failed-provisions")
    assert resp.status_code == 200
    body = resp.json()
    assert body["degraded"] is True
    assert body["jobs"] == []


def test_limit_query_param_caps_results(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Requesting `limit=1` returns exactly the most recent failure."""
    rows = [
        _make_state(job_id=f"j-{i}", job_type="aks_provision", status="failed", hours_ago=i)
        for i in range(1, 6)
    ]
    _patch_repo(monkeypatch, rows)
    resp = client.get("/api/aks/recent-failed-provisions?hours=24&limit=1")
    assert resp.status_code == 200
    jobs = resp.json()["jobs"]
    assert len(jobs) == 1
    assert jobs[0]["job_id"] == "j-1"  # newest (smallest hours_ago)
