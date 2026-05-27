"""Tests for the OpenAPI deploy status envelope, the new cancel route, and
the expanded proxy deny-list.

Responsibility: Lock the route → SPA contract for three additions made in
    the 2026-05-27 OpenAPI menu audit follow-up: (1) ``recovery_action`` /
    ``recovery_hint`` injection on upstream-reach failures, (2) the new
    ``POST /aks/openapi/deploy/{task_id}/cancel`` route that mirrors
    cancel-provision, and (3) the dashed deny-list siblings on the proxy.
Edit boundaries: Pure unit tests with a fake ``AsyncResult`` and stubbed
    JobStateRepository. No live broker, no Azure SDK, no httpx.
Key entry points: see per-test docstrings.
Risky contracts: The SPA reads ``recovery_action == "peer_with_platform"``
    at the envelope root to render the "Repair VNet peering" button —
    breaking that field name silently degrades the recovery affordance.
Validation: ``uv run pytest -q api/tests/test_openapi_deploy_status_and_cancel.py``.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setenv("API_CLIENT_ID", "00000000-0000-0000-0000-000000000000")
    from api.main import app

    return TestClient(app)


def _patch_async_result(
    monkeypatch: pytest.MonkeyPatch,
    *,
    status: str,
    successful: bool = False,
    result: Any = None,
    info: Any = None,
) -> None:
    """Install a fake ``celery.result.AsyncResult`` so the status route
    sees a deterministic payload without standing up a broker.
    """

    class _FakeAsyncResult:
        def __init__(self, tid: str, app: Any | None = None) -> None:
            self.task_id = tid
            self.status = status
            self._result = result
            self._info = info
            self._successful = successful
            self._ready = status in {"SUCCESS", "FAILURE", "REVOKED"}

        def ready(self) -> bool:
            return self._ready

        def successful(self) -> bool:
            return self._successful

        @property
        def result(self) -> Any:
            return self._result

        @property
        def info(self) -> Any:
            return self._info

    import celery.result

    monkeypatch.setattr(celery.result, "AsyncResult", _FakeAsyncResult)


# ---------------------------------------------------------------------------
# /openapi/deploy/{id}/status — recovery_action injection
# ---------------------------------------------------------------------------


def test_status_injects_peer_with_platform_when_external_ip_empty(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed task whose deploy payload has no ``external_ip`` is the
    canonical "LB never came up" / VNet-peering symptom — the SPA must
    receive ``recovery_action: peer_with_platform`` so it can render the
    Repair button without parsing the free-form error string."""
    payload = {
        "status": "failed",
        "cluster_name": "aks-elb",
        "openapi_deploy": {
            "status": "no_ready_replica",
            "image": "elbacr.azurecr.io/elb-openapi:4.14",
            "external_ip": "",
            "ready_replicas": 0,
            "desired_replicas": 1,
            "error": "Deployment applied but no pod reached Ready.",
            "diagnostics": {
                "likely_cause": "unknown",
                "message": "Deployment applied but no pod reached Ready.",
                "events": [],
            },
        },
    }
    _patch_async_result(
        monkeypatch, status="SUCCESS", successful=True, result=payload
    )

    response = client.get("/api/aks/openapi/deploy/task-no-ip/status")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["runtime_status"] == "Completed"
    assert body["output"]["status"] == "failed"
    assert body["recovery_action"] == "peer_with_platform"
    assert "elb-openapi" in body["recovery_hint"]


def test_status_injects_peer_with_platform_on_no_endpoints_event(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``no endpoints available`` events surface from K8s when the
    Service exists but cannot route to the pod — same recovery hint."""
    payload = {
        "status": "failed",
        "openapi_deploy": {
            "status": "no_ready_replica",
            "external_ip": "10.0.0.50",
            "error": "Deployment applied but no pod reached Ready.",
            "diagnostics": {
                "likely_cause": "unknown",
                "events": [
                    {
                        "namespace": "default",
                        "kind": "Endpoints",
                        "name": "elb-openapi",
                        "reason": "FailedToCreateEndpoint",
                        "message": "no endpoints available for service elb-openapi",
                    }
                ],
            },
        },
    }
    _patch_async_result(
        monkeypatch, status="SUCCESS", successful=True, result=payload
    )

    body = client.get("/api/aks/openapi/deploy/task-no-eps/status").json()
    assert body["recovery_action"] == "peer_with_platform"


def test_status_injects_peer_with_platform_on_upstream_error_phrase(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REVOKED / FAILURE branch (no structured payload, just an error
    string) still gets the recovery hint when the message indicates the
    upstream was unreachable."""
    _patch_async_result(
        monkeypatch,
        status="FAILURE",
        successful=False,
        result=RuntimeError("k8s api connection refused after 30s"),
    )

    body = client.get("/api/aks/openapi/deploy/task-revoked/status").json()
    assert body["runtime_status"] == "Failed"
    assert body["recovery_action"] == "peer_with_platform"


def test_status_omits_recovery_hint_for_image_pull_failure(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ImagePullBackOff is NOT a peering issue — the SPA would mislead the
    operator by rendering the Repair button. The hint must be absent."""
    payload = {
        "status": "failed",
        "openapi_deploy": {
            "status": "no_ready_replica",
            "external_ip": "10.0.0.50",  # LB came up fine
            "error": "Image pull failed: AcrPull not granted on kubelet identity.",
            "diagnostics": {
                "likely_cause": "image_pull_failed",
                "events": [
                    {
                        "reason": "Failed",
                        "message": (
                            "ErrImagePull: pull access denied for "
                            "elbacr.azurecr.io/elb-openapi"
                        ),
                    }
                ],
            },
        },
    }
    _patch_async_result(
        monkeypatch, status="SUCCESS", successful=True, result=payload
    )

    body = client.get("/api/aks/openapi/deploy/task-imgpull/status").json()
    assert "recovery_action" not in body
    assert "recovery_hint" not in body


def test_status_omits_recovery_hint_on_success(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Successful deploy must not carry the recovery hint."""
    payload = {
        "status": "succeeded",
        "openapi_deploy": {
            "status": "deployed",
            "external_ip": "10.0.0.50",
            "ready_replicas": 1,
            "desired_replicas": 1,
        },
    }
    _patch_async_result(
        monkeypatch, status="SUCCESS", successful=True, result=payload
    )

    body = client.get("/api/aks/openapi/deploy/task-ok/status").json()
    assert body["output"]["status"] == "succeeded"
    assert "recovery_action" not in body


def test_status_omits_recovery_hint_while_running(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A still-running task has no failure to classify yet."""
    _patch_async_result(
        monkeypatch, status="STARTED", info={"phase": "waiting_for_external_ip"}
    )

    body = client.get("/api/aks/openapi/deploy/task-running/status").json()
    assert body["runtime_status"] == "Running"
    assert body["output"] is None
    assert "recovery_action" not in body


# ---------------------------------------------------------------------------
# POST /openapi/deploy/{task_id}/cancel
# ---------------------------------------------------------------------------


def _patch_cancel_deps(
    monkeypatch: pytest.MonkeyPatch,
    *,
    status: str,
    owner_oid: str | None = None,
    job_id: str | None = "job-openapi-1",
    revoke_calls: list[dict[str, Any]] | None = None,
    update_calls: list[dict[str, Any]] | None = None,
) -> None:
    """Stub the AsyncResult, Celery control, state repo, and update_state
    helper used by the new cancel route.
    """

    class _FakeAsyncResult:
        def __init__(self, tid: str, app: Any | None = None) -> None:
            self.task_id = tid
            self.status = status

    class _FakeControl:
        def revoke(self, tid: str, **kwargs: Any) -> None:
            if revoke_calls is not None:
                revoke_calls.append({"task_id": tid, **kwargs})

    class _FakeCeleryApp:
        control = _FakeControl()

    import api.celery_app as celery_app_mod
    import api.routes.aks.cancel as cancel_mod
    import api.services.state_repo as state_repo_mod
    import api.tasks.azure.helpers as azure_helpers_mod
    import celery.result

    monkeypatch.setattr(celery.result, "AsyncResult", _FakeAsyncResult)
    # The new cancel route imports ``celery_app`` lazily from
    # ``api.celery_app`` — patch the source module so the bound name the
    # route sees is the fake (cancel-provision's twin route is patched
    # similarly in test_aks_cancel_provision.py).
    monkeypatch.setattr(celery_app_mod, "celery_app", _FakeCeleryApp())
    monkeypatch.setattr(cancel_mod, "celery_app", _FakeCeleryApp())

    class _FakeState:
        def __init__(self) -> None:
            self.job_id = job_id
            self.owner_oid = owner_oid

    class _FakeRepo:
        def find_by_task_id(self, _task_id: str) -> _FakeState | None:
            if job_id is None:
                return None
            return _FakeState()

    monkeypatch.setattr(state_repo_mod, "JobStateRepository", lambda: _FakeRepo())
    # The cancel.py ownership helper imports JobStateRepository at module
    # scope — patch the bound name there as well so dev-bypass auth still
    # exercises the helper's guard logic.
    monkeypatch.setattr(cancel_mod, "JobStateRepository", lambda: _FakeRepo())

    def _capture(jid: str, phase: str, status: str = "running", **extra: Any) -> None:
        if update_calls is not None:
            update_calls.append({"job_id": jid, "phase": phase, "status": status, **extra})

    monkeypatch.setattr(azure_helpers_mod, "update_state", _capture)


def test_cancel_revokes_running_deploy(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A STARTED task is revoked with terminate=True and the JobState row
    (if any) is flipped to status='cancelled'."""
    revoke_calls: list[dict[str, Any]] = []
    update_calls: list[dict[str, Any]] = []
    _patch_cancel_deps(
        monkeypatch,
        status="STARTED",
        revoke_calls=revoke_calls,
        update_calls=update_calls,
    )

    resp = client.post("/api/aks/openapi/deploy/task-openapi-abc/cancel")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["task_id"] == "task-openapi-abc"
    assert body["was_running"] is True
    assert body["cancelled"] is True
    assert body["previous_status"] == "STARTED"
    # OpenAPI deploy probes yield faster than the AKS ARM poll, so the
    # documented settle window is 10 s (not 20 s).
    assert body["settle_after_seconds"] == 10
    assert revoke_calls == [
        {"task_id": "task-openapi-abc", "terminate": True, "signal": "SIGTERM"}
    ]
    assert update_calls == [
        {
            "job_id": "job-openapi-1",
            "phase": "cancelled_by_user",
            "status": "cancelled",
            "error_code": "cancelled_by_user",
        }
    ]


def test_cancel_is_idempotent_on_terminal_states(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SUCCESS / FAILURE / REVOKED tasks return 200 with ``was_running=False``
    and never re-invoke revoke()."""
    revoke_calls: list[dict[str, Any]] = []
    _patch_cancel_deps(monkeypatch, status="SUCCESS", revoke_calls=revoke_calls)

    resp = client.post("/api/aks/openapi/deploy/task-done/cancel")

    assert resp.status_code == 200
    body = resp.json()
    assert body["was_running"] is False
    assert body["previous_status"] == "SUCCESS"
    assert body["settle_after_seconds"] == 0
    assert revoke_calls == []


def test_cancel_passes_through_when_no_state_row(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OpenAPI deploy does not currently persist a JobState row; the
    cancel route must still revoke Celery and respond with job_id=None."""
    revoke_calls: list[dict[str, Any]] = []
    _patch_cancel_deps(
        monkeypatch,
        status="STARTED",
        job_id=None,
        revoke_calls=revoke_calls,
    )

    resp = client.post("/api/aks/openapi/deploy/task-orphan/cancel")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["was_running"] is True
    assert body["job_id"] is None
    assert revoke_calls and revoke_calls[0]["terminate"] is True


def test_cancel_rejects_non_owner(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the JobState carries a different ``owner_oid`` than the
    caller, the request is rejected with 403. Mirrors cancel-provision."""
    _patch_cancel_deps(
        monkeypatch,
        status="STARTED",
        owner_oid="other-user-oid",
    )

    resp = client.post("/api/aks/openapi/deploy/task-other/cancel")
    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# Proxy deny-list — new dashed siblings
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/v1/debug-info",
        "/v1/private-keys",
        "/v1/sudo-mode/promote",
    ],
)
def test_openapi_proxy_rejects_new_dashed_deny_tokens(
    client: TestClient,
    path: str,
) -> None:
    """The new ``/debug-``, ``/private-``, ``/sudo-`` siblings keep the
    defence-in-depth coverage symmetric with the existing ``/admin-`` and
    ``/internal-`` tokens — any allowlisted prefix that surfaces a
    dashed admin / debug / private / sudo segment must be rejected."""
    resp = client.get(
        "/api/aks/openapi/proxy",
        params={"resource_group": "rg-elb", "cluster_name": "aks-elb", "path": path},
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == "openapi_path_not_allowlisted"


def test_openapi_proxy_denied_tokens_keep_symmetric_coverage() -> None:
    """Each privileged family must carry three-way coverage:
    ``/x/`` (segment), ``/x?`` (query-stripped exact), ``/x-`` (dashed
    sibling). Drift would silently shrink the defence-in-depth surface."""
    from api.routes.aks.openapi import _OPENAPI_PROXY_DENIED_PATH_TOKENS

    families = ("admin", "internal", "debug", "private", "sudo")
    for family in families:
        for suffix in ("/", "?", "-"):
            token = f"/{family}{suffix}"
            assert token in _OPENAPI_PROXY_DENIED_PATH_TOKENS, (
                f"missing deny token {token!r}"
            )
