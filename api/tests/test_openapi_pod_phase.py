"""Tests for the elb-openapi pod startup-state classifier and spec-route wiring.

Responsibility: Verify ``classify_openapi_pod_state`` distinguishes starting vs
failed vs ready vs absent, and that ``GET /api/aks/openapi/spec`` surfaces a
non-peering ``openapi_pod_starting`` degraded payload while the pod is booting.
Edit boundaries: Keep assertions on the classifier + route behaviour; use fakes,
no live Azure / Kubernetes calls.
Key entry points: ``classify_openapi_pod_state``, ``get_openapi_pod_startup_state``,
``/api/aks/openapi/spec``.
Risky contracts: The ``state`` strings and ``degraded_reason`` values are part of
the SPA contract; keep them stable.
Validation: ``uv run pytest -q api/tests/test_openapi_pod_phase.py``.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from api.services.openapi.pod_phase import classify_openapi_pod_state
from fastapi.testclient import TestClient


def _pod(
    *,
    phase: str = "Pending",
    waiting_reason: str | None = None,
    ready: bool = False,
) -> dict[str, Any]:
    container: dict[str, Any] = {"ready": ready, "state": {}}
    if waiting_reason is not None:
        container["state"] = {"waiting": {"reason": waiting_reason}}
    return {"status": {"phase": phase, "containerStatuses": [container]}}


# --------------------------------------------------------------------------- #
# Pure classifier
# --------------------------------------------------------------------------- #
def test_classify_ready_when_replica_ready() -> None:
    state, reason, _ = classify_openapi_pod_state([], ready_replicas=1, desired_replicas=1)
    assert state == "ready"
    assert reason == ""


def test_classify_container_creating_is_starting() -> None:
    pods = [_pod(phase="Pending", waiting_reason="ContainerCreating")]
    state, reason, message = classify_openapi_pod_state(
        pods, ready_replicas=0, desired_replicas=1
    )
    assert state == "starting"
    assert reason == "ContainerCreating"
    assert "starting" in message.lower()


def test_classify_pending_without_status_is_starting() -> None:
    pods = [{"status": {"phase": "Pending", "containerStatuses": []}}]
    state, _, _ = classify_openapi_pod_state(pods, ready_replicas=0, desired_replicas=1)
    assert state == "starting"


def test_classify_running_not_ready_is_starting() -> None:
    # Container is Running but not ready yet (readiness probe warming).
    pods = [_pod(phase="Running", waiting_reason=None, ready=False)]
    state, _, _ = classify_openapi_pod_state(pods, ready_replicas=0, desired_replicas=1)
    assert state == "starting"


def test_classify_crashloop_is_failed() -> None:
    pods = [_pod(phase="Running", waiting_reason="CrashLoopBackOff")]
    state, reason, message = classify_openapi_pod_state(
        pods, ready_replicas=0, desired_replicas=1
    )
    assert state == "failed"
    assert reason == "CrashLoopBackOff"
    assert "logs" in message.lower()


def test_classify_image_pull_backoff_is_failed() -> None:
    pods = [_pod(phase="Pending", waiting_reason="ImagePullBackOff")]
    state, reason, _ = classify_openapi_pod_state(pods, ready_replicas=0, desired_replicas=1)
    assert state == "failed"
    assert reason == "ImagePullBackOff"


def test_classify_no_pods_no_desired_is_absent() -> None:
    state, _, _ = classify_openapi_pod_state([], ready_replicas=0, desired_replicas=0)
    assert state == "absent"


def test_classify_no_pods_but_desired_is_starting() -> None:
    state, _, _ = classify_openapi_pod_state([], ready_replicas=0, desired_replicas=1)
    assert state == "starting"


def test_classify_failed_wins_over_starting_across_pods() -> None:
    pods = [
        _pod(phase="Pending", waiting_reason="ContainerCreating"),
        _pod(phase="Running", waiting_reason="CrashLoopBackOff"),
    ]
    state, reason, _ = classify_openapi_pod_state(pods, ready_replicas=0, desired_replicas=2)
    assert state == "failed"
    assert reason == "CrashLoopBackOff"


# --------------------------------------------------------------------------- #
# Spec route wiring
# --------------------------------------------------------------------------- #
@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setenv("AZURE_TENANT_ID", "common")
    monkeypatch.setenv("API_CLIENT_ID", "00000000-0000-0000-0000-000000000000")
    monkeypatch.setattr(
        "api.services.openapi.runtime.get_openapi_public_base_url",
        lambda **_kwargs: {},
    )
    from api.main import app

    return TestClient(app)


def _force_spec_fetch_to_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    """Service IP resolves but both the direct fetch and the k8s service-proxy
    fallback fail, so the route reaches the startup-state probe."""

    import api.services as services
    from api.routes.aks import openapi as openapi_route
    from api.services import httpx_pool
    from api.services.k8s import monitoring as k8s_monitoring

    monkeypatch.setattr(services, "get_credential", lambda: object())
    monkeypatch.setattr(k8s_monitoring, "k8s_get_service_ip", lambda *_a, **_k: "10.0.0.50")

    class BrokenClient:
        def get(self, _url: str) -> httpx.Response:
            raise httpx.ConnectTimeout("no route (mocked)")

    monkeypatch.setattr(httpx_pool, "get_pooled_client", lambda *_a, **_k: BrokenClient())
    monkeypatch.setattr(
        openapi_route, "_fetch_openapi_spec_via_k8s_proxy", lambda *_a, **_k: None
    )


def test_spec_route_reports_pod_starting_without_peering_hint(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_spec_fetch_to_fail(monkeypatch)
    monkeypatch.setattr(
        "api.services.openapi.pod_phase.get_openapi_pod_startup_state",
        lambda *_a, **_k: {
            "state": "starting",
            "reason": "ContainerCreating",
            "message": "The elb-openapi pod is starting (ContainerCreating).",
            "ready_replicas": 0,
            "desired_replicas": 1,
        },
    )

    response = client.get(
        "/api/aks/openapi/spec",
        params={"resource_group": "rg-elb", "cluster_name": "aks-elb"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["degraded"] is True
    assert body["degraded_reason"] == "openapi_pod_starting"
    assert body["pod_state"] == "starting"
    assert body["pod_reason"] == "ContainerCreating"
    # The whole point: a starting pod must NOT carry the peering-repair
    # affordance, otherwise the SPA renders a misleading red error.
    assert "recovery_action" not in body


def test_spec_route_reports_pod_not_ready_for_failed_pod(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_spec_fetch_to_fail(monkeypatch)
    monkeypatch.setattr(
        "api.services.openapi.pod_phase.get_openapi_pod_startup_state",
        lambda *_a, **_k: {
            "state": "failed",
            "reason": "CrashLoopBackOff",
            "message": "The elb-openapi pod is not ready (CrashLoopBackOff).",
            "ready_replicas": 0,
            "desired_replicas": 1,
        },
    )

    response = client.get(
        "/api/aks/openapi/spec",
        params={"resource_group": "rg-elb", "cluster_name": "aks-elb"},
    )

    body = response.json()
    assert body["degraded_reason"] == "openapi_pod_not_ready"
    assert body["pod_reason"] == "CrashLoopBackOff"
    assert "recovery_action" not in body


def test_spec_route_keeps_peering_hint_when_pod_ready_but_unreachable(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Ready pod whose endpoint the api sidecar cannot reach is the genuine
    VNet-peering case — the peering-repair affordance must still appear."""

    _force_spec_fetch_to_fail(monkeypatch)
    monkeypatch.setattr(
        "api.services.openapi.pod_phase.get_openapi_pod_startup_state",
        lambda *_a, **_k: {
            "state": "ready",
            "reason": "",
            "message": "elb-openapi has a Ready replica.",
            "ready_replicas": 1,
            "desired_replicas": 1,
        },
    )

    response = client.get(
        "/api/aks/openapi/spec",
        params={"resource_group": "rg-elb", "cluster_name": "aks-elb"},
    )

    body = response.json()
    assert body["degraded_reason"] == "openapi_endpoint_unreachable"
    assert body["recovery_action"] == "peer_with_platform"


def test_spec_route_keeps_peering_hint_when_probe_unknown(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the probe cannot determine the state (k8s unreachable), fall back to
    the existing peering-repair behaviour rather than guessing 'starting'."""

    _force_spec_fetch_to_fail(monkeypatch)
    monkeypatch.setattr(
        "api.services.openapi.pod_phase.get_openapi_pod_startup_state",
        lambda *_a, **_k: {
            "state": "unknown",
            "reason": "",
            "message": "elb-openapi pod state could not be determined.",
            "ready_replicas": 0,
            "desired_replicas": 0,
        },
    )

    response = client.get(
        "/api/aks/openapi/spec",
        params={"resource_group": "rg-elb", "cluster_name": "aks-elb"},
    )

    body = response.json()
    assert body["degraded_reason"] == "openapi_endpoint_unreachable"
    assert body["recovery_action"] == "peer_with_platform"
