"""Tests for the elb-openapi rebuild + redeploy orchestration task and routes.

Responsibility: Pin the charter rollout-order gate — the orchestrator enqueues
    ``deploy_openapi_service`` ONLY when the ACR build reaches ``Succeeded``; a
    failed, timed-out, or unscheduled build returns a terminal ``failed`` payload
    and never deploys. Also pin the bounded poll, the ``dry_run`` no-side-effect
    path, and the route validation + envelope shaping.
Edit boundaries: Task + route behaviour only. The ACR build scheduling, the ACR
    run poll, the deploy enqueue, and the route's Celery enqueue are all
    monkeypatched so the suite never touches Azure or a live broker.
Key entry points: the ``test_*`` functions.
Risky contracts: deploy-only-on-success gate, bounded poll, dry_run side-effect
    freedom, route 400 on missing params.
Validation: ``uv run pytest -q api/tests/test_openapi_rebuild.py``.
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from api.tasks.openapi import rebuild as rebuild_mod


def _apply(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> dict[str, Any]:
    """Run the task synchronously (eager ``.apply``) with patched helpers.

    Captures schedule + deploy-enqueue calls via closures and returns the task
    result dict. ``overrides`` may set ``run_id`` / ``build_status`` /
    ``deploy_task_id`` / ``dry_run`` / ``storage_account``.
    """
    scheduled: list[dict[str, Any]] = []
    deployed: list[dict[str, Any]] = []

    def fake_schedule(sub: str, rg: str, registry: str) -> str:
        scheduled.append({"sub": sub, "rg": rg, "registry": registry})
        return overrides.get("run_id", "run-123")

    def fake_poll(*_a: Any, **_k: Any) -> str:
        return overrides.get("build_status", "Succeeded")

    def fake_enqueue(**kwargs: Any) -> str:
        deployed.append(kwargs)
        return overrides.get("deploy_task_id", "deploy-789")

    monkeypatch.setattr(rebuild_mod, "_schedule_openapi_build", fake_schedule)
    monkeypatch.setattr(rebuild_mod, "_poll_acr_build", fake_poll)
    monkeypatch.setattr(rebuild_mod, "_enqueue_openapi_deploy", fake_enqueue)
    monkeypatch.setattr(
        "api.services.acr_build_state.record_pending_build", lambda *a, **k: None
    )

    kwargs: dict[str, Any] = {
        "subscription_id": "sub-1",
        "resource_group": "rg-cluster",
        "cluster_name": "elb-cluster-01",
        "acr_name": "myacr",
        "acr_resource_group": "rg-acr",
    }
    for key in ("dry_run", "storage_account"):
        if key in overrides:
            kwargs[key] = overrides[key]

    # ``.apply`` runs the task body inline (eager EagerResult) so ``self`` is the
    # real task and ``record_progress`` is a safe no-op — no broker needed.
    result = rebuild_mod.rebuild_and_redeploy_openapi.apply(kwargs=kwargs).result
    assert isinstance(result, dict), result
    result["_scheduled"] = scheduled
    result["_deployed"] = deployed
    return result


def test_build_success_enqueues_deploy(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _apply(monkeypatch, build_status="Succeeded")
    assert out["status"] == "deploy_enqueued"
    assert out["deploy_task_id"] == "deploy-789"
    assert out["build_run_id"] == "run-123"
    assert out["deploy_status_url"] == "/api/aks/openapi/deploy/deploy-789/status"
    assert len(out["_deployed"]) == 1
    # ACR RG forwarded to the deploy task.
    assert out["_deployed"][0]["acr_resource_group"] == "rg-acr"


def test_build_failure_does_not_deploy(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _apply(monkeypatch, build_status="Failed")
    assert out["status"] == "failed"
    assert out["stage"] == "build"
    assert out["error_code"] == "acr_build_failed"
    assert out["build_status"] == "Failed"
    assert out["_deployed"] == []  # the gate held — no deploy


def test_build_timeout_does_not_deploy(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _apply(monkeypatch, build_status=rebuild_mod._BUILD_TIMEOUT)
    assert out["status"] == "failed"
    assert out["error_code"] == "build_timeout"
    assert out["_deployed"] == []


def test_build_schedule_failure_does_not_deploy(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _apply(monkeypatch, run_id="")  # schedule returned no run_id
    assert out["status"] == "failed"
    assert out["error_code"] == "build_schedule_failed"
    assert out["_deployed"] == []


def test_deploy_enqueue_failure_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _apply(monkeypatch, build_status="Succeeded", deploy_task_id="")
    assert out["status"] == "failed"
    assert out["stage"] == "deploy"
    assert out["error_code"] == "deploy_enqueue_failed"


def test_dry_run_has_no_side_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _apply(monkeypatch, dry_run=True)
    assert out["status"] == "dry_run"
    assert out["image"].startswith("elb-openapi:")
    assert out["would_build"] is True
    assert out["_scheduled"] == []
    assert out["_deployed"] == []


def test_image_tag_is_the_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.services.image_tags import IMAGE_TAGS

    out = _apply(monkeypatch, dry_run=True)
    assert out["image"] == f"elb-openapi:{IMAGE_TAGS['elb-openapi']}"


def _patch_acr_client(monkeypatch: pytest.MonkeyPatch, status_value: str) -> None:
    class _Run:
        status = status_value

    class _Runs:
        def get(self, *_a: Any, **_k: Any) -> Any:
            return _Run()

    class _Client:
        runs = _Runs()

        def __init__(self, *_a: Any, **_k: Any) -> None:
            pass

    # ``get_credential()`` is left real (lazy DefaultAzureCredential, no token
    # fetched because the fake client ignores the credential), so this stays out
    # of the facade-contract monkeypatch surface.
    monkeypatch.setattr(
        "azure.mgmt.containerregistry.ContainerRegistryManagementClient", _Client
    )


def test_poll_returns_succeeded(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_acr_client(monkeypatch, "Succeeded")
    status = rebuild_mod._poll_acr_build("s", "rg", "reg", "run-1", deadline_seconds=5)
    assert status == "Succeeded"


def test_poll_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_acr_client(monkeypatch, "Running")
    monkeypatch.setattr(rebuild_mod.time, "sleep", lambda *_a: None)
    status = rebuild_mod._poll_acr_build(
        "s", "rg", "reg", "run-1", deadline_seconds=1, interval_seconds=1
    )
    assert status == rebuild_mod._BUILD_TIMEOUT


# ── route ──────────────────────────────────────────────────────────────────


def _client():
    os.environ.setdefault("AUTH_DEV_BYPASS", "true")
    from api.main import app
    from fastapi.testclient import TestClient

    return TestClient(app)


def test_route_enqueues_and_returns_task_id(monkeypatch: pytest.MonkeyPatch) -> None:
    # Patch the route's Celery enqueue so no broker is needed.
    monkeypatch.setattr(
        "api.routes.aks.openapi._safe_delay",
        lambda *a, **k: type("R", (), {"id": "task-xyz"})(),
    )
    client = _client()
    r = client.post(
        "/api/aks/openapi/rebuild-deploy",
        json={
            "subscription_id": "s",
            "resource_group": "rg-cluster",
            "cluster_name": "elb-cluster-01",
            "acr_name": "myacr",
            "dry_run": True,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["task_id"] == "task-xyz"
    assert body["statusQueryGetUri"] == "/api/aks/openapi/rebuild-deploy/task-xyz/status"


def test_route_requires_core_params() -> None:
    client = _client()
    r = client.post("/api/aks/openapi/rebuild-deploy", json={"acr_name": "myacr"})
    assert r.status_code == 400
    # HTTPException(detail={"code": ...}) is flattened to a top-level code by the
    # app's exception handler (same shape as the other 400 routes).
    assert r.json()["code"] == "missing_parameters"
