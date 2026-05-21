"""Tests for the `/api/blast/jobs/<id>/execution-steps` endpoint.

Responsibility: Lock the precedence contract that the execution-steps
endpoint serves LIVE Table state in preference to the persisted snapshot
blob. The blob is written once by ``finalize_job_artifacts`` and can be
silently stale after reconcile beats backfill trailing fields
(K8s pod log tails on ``running.last_output``, …); only the live state
sees those updates.
Edit boundaries: Test only the route's precedence + fallback contract.
Key entry points: `test_execution_steps_prefers_live_state_over_snapshot`,
`test_execution_steps_falls_back_to_snapshot_when_live_unavailable`.
Risky contracts: The route imports `api.services.job_artifacts` and
`api.services.state_repo` lazily; monkeypatch through `sys.modules` after
the route has been touched.
Validation: `uv run pytest -q api/tests/test_blast_execution_steps_route.py`.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("AZURE_TENANT_ID", "common")
    monkeypatch.setenv("API_CLIENT_ID", "00000000-0000-0000-0000-000000000000")
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app

    return TestClient(app)


def _summary(**overrides):
    base = {"job_id": "job-1", "owner_oid": ""}
    base.update(overrides)
    return SimpleNamespace(**base)


def _state(running_last_output: str = "live tail with K8s pod logs"):
    return SimpleNamespace(
        job_id="job-1",
        status="completed",
        phase="completed",
        created_at="2026-05-22T00:00:00Z",
        updated_at="2026-05-22T00:01:30Z",
        payload={
            "_progress": {
                "phase": "completed",
                "status": "completed",
                "steps": {
                    "running": {
                        "phase": "running",
                        "status": "completed",
                        "last_output": running_last_output,
                    },
                },
            }
        },
    )


def _stub_repo(monkeypatch: pytest.MonkeyPatch, *, summary, state) -> None:
    repo = SimpleNamespace(
        get_summary=lambda _job_id: summary,
        get=lambda _job_id: state,
    )
    monkeypatch.setattr(
        "api.services.state_repo.get_state_repo",
        lambda: repo,
        raising=False,
    )


def test_execution_steps_prefers_live_state_over_snapshot(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Snapshot blob carries an old/empty pod-log tail (the partial capture
    # frozen by finalize_job_artifacts at completion time).
    stale_snapshot = {
        "job_id": "job-1",
        "phase": "completed",
        "status": "completed",
        "custom_status": {
            "phase": "completed",
            "status": "completed",
            "steps": {"running": {"status": "completed", "last_output": ""}},
        },
        "output": {
            "phase": "completed",
            "status": "completed",
            "steps": {"running": {"status": "completed", "last_output": ""}},
        },
    }
    # Live state has the trailing K8s pod log that reconcile backfilled.
    live_tail = "--- blastn-batch-s00-job-000-abcdef-xxxxx/blast ---\nBASH 5.1"
    _stub_repo(
        monkeypatch,
        summary=_summary(),
        state=_state(running_last_output=live_tail),
    )
    monkeypatch.setattr(
        "api.services.job_artifacts.read_execution_steps_snapshot",
        lambda _job_id: stale_snapshot,
        raising=False,
    )
    monkeypatch.setattr(
        "api.services.job_artifacts.artifact_state_payload",
        lambda _job_id, _type: {"artifact_state": "ready"},
        raising=False,
    )

    r = client.get("/api/blast/jobs/job-1/execution-steps")
    assert r.status_code == 200, r.text
    body = r.json()
    steps = (body.get("output") or {}).get("steps") or {}
    assert (
        steps["running"]["last_output"] == live_tail
    ), "live state must win over the frozen snapshot blob"
    assert body.get("artifact_state") == "ready"


def test_execution_steps_falls_back_to_snapshot_when_live_unavailable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Live Table read raises (e.g. transient Storage outage); the route must
    # not 500 — fall back to the persisted snapshot blob.
    repo = SimpleNamespace(
        get_summary=lambda _job_id: _summary(),
        get=lambda _job_id: (_ for _ in ()).throw(RuntimeError("table down")),
    )
    monkeypatch.setattr(
        "api.services.state_repo.get_state_repo",
        lambda: repo,
        raising=False,
    )
    snapshot = {
        "job_id": "job-1",
        "phase": "completed",
        "status": "completed",
        "custom_status": {
            "phase": "completed",
            "status": "completed",
            "steps": {"running": {"status": "completed", "last_output": "snap"}},
        },
        "output": {"steps": {"running": {"last_output": "snap"}}},
    }
    monkeypatch.setattr(
        "api.services.job_artifacts.read_execution_steps_snapshot",
        lambda _job_id: snapshot,
        raising=False,
    )

    r = client.get("/api/blast/jobs/job-1/execution-steps")
    assert r.status_code == 200, r.text
    body = r.json()
    assert (
        (body.get("output") or {}).get("steps", {}).get("running", {}).get("last_output")
        == "snap"
    )
    assert body.get("artifact_state") == "ready"


def test_execution_steps_returns_404_when_job_missing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = SimpleNamespace(
        get_summary=lambda _job_id: None,
        get=lambda _job_id: None,
    )
    monkeypatch.setattr(
        "api.services.state_repo.get_state_repo",
        lambda: repo,
        raising=False,
    )
    r = client.get("/api/blast/jobs/missing-job/execution-steps")
    assert r.status_code == 404
