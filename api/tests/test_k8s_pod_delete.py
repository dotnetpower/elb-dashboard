"""Unit tests for `k8s_pod_delete` service helper.

Verifies the system-namespace gate, status-code mapping, and grace-period
clamping in isolation by stubbing `_get_k8s_session` so no Kubernetes API
is contacted.

Responsibility: Cover the safety + response-shape contracts of
`api.services.k8s.observability.k8s_pod_delete`.
Edit boundaries: Pure unit tests — no real credentials, no real K8s API.
Key entry points: `test_refuses_system_namespaces`,
`test_returns_deleted_on_2xx`, `test_returns_not_found_on_404`,
`test_propagates_error_payload`.
Risky contracts: The system-namespace gate is load-bearing — frontend
also hides the button, but the backend must remain the authoritative
gate (OWASP A01).
Validation: `uv run pytest -q api/tests/test_k8s_pod_delete.py`.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from api.services.k8s import observability as obs


class _FakeResponse:
    def __init__(self, status_code: int, text: str = "") -> None:
        self.status_code = status_code
        self.text = text


class _FakeSession:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response
        self.closed = False
        self.last_call: dict[str, Any] | None = None

    def delete(self, url: str, *, params: dict[str, Any], timeout: int) -> _FakeResponse:
        self.last_call = {"url": url, "params": params, "timeout": timeout}
        return self._response

    def close(self) -> None:
        self.closed = True


def _patch_session(monkeypatch: pytest.MonkeyPatch, response: _FakeResponse) -> _FakeSession:
    """Replace _get_k8s_session with a stub that returns ``(session, server)``."""
    session = _FakeSession(response)

    def _fake_get_session(*_args: Any, **_kwargs: Any) -> tuple[_FakeSession, str]:
        return session, "https://example.test"

    # k8s_pod_delete imports lazily from api.services.k8s.monitoring so we
    # patch that module's symbol.
    from api.services.k8s import monitoring as monitoring_module

    monkeypatch.setattr(monitoring_module, "_get_k8s_session", _fake_get_session)
    return session


@pytest.mark.parametrize("namespace", sorted(obs.SYSTEM_NAMESPACES))
def test_refuses_system_namespaces(namespace: str) -> None:
    """The backend must reject system namespaces regardless of frontend gating."""
    with pytest.raises(PermissionError):
        obs.k8s_pod_delete(
            credential=SimpleNamespace(),  # type: ignore[arg-type]
            subscription_id="sub",
            resource_group="rg",
            cluster_name="cluster",
            namespace=namespace,
            pod_name="some-pod",
        )


def test_rejects_invalid_namespace_or_pod_name() -> None:
    with pytest.raises(ValueError):
        obs.k8s_pod_delete(
            credential=SimpleNamespace(),  # type: ignore[arg-type]
            subscription_id="sub",
            resource_group="rg",
            cluster_name="cluster",
            namespace="UPPERCASE",  # not RFC 1123
            pod_name="ok",
        )


def test_rejects_out_of_range_grace_period() -> None:
    with pytest.raises(ValueError):
        obs.k8s_pod_delete(
            credential=SimpleNamespace(),  # type: ignore[arg-type]
            subscription_id="sub",
            resource_group="rg",
            cluster_name="cluster",
            namespace="default",
            pod_name="some-pod",
            grace_period_seconds=-1,
        )


def test_returns_deleted_on_2xx(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _patch_session(monkeypatch, _FakeResponse(202))
    result = obs.k8s_pod_delete(
        credential=SimpleNamespace(),  # type: ignore[arg-type]
        subscription_id="sub",
        resource_group="rg",
        cluster_name="cluster",
        namespace="default",
        pod_name="elb-finalizer-abc",
    )
    assert result["status"] == "deleted"
    assert result["namespace"] == "default"
    assert result["pod"] == "elb-finalizer-abc"
    assert result["status_code"] == 202
    assert session.closed is True
    assert session.last_call is not None
    assert session.last_call["url"].endswith(
        "/api/v1/namespaces/default/pods/elb-finalizer-abc"
    )
    assert session.last_call["params"]["propagationPolicy"] == "Background"


def test_returns_not_found_on_404(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_session(monkeypatch, _FakeResponse(404, text="not found"))
    result = obs.k8s_pod_delete(
        credential=SimpleNamespace(),  # type: ignore[arg-type]
        subscription_id="sub",
        resource_group="rg",
        cluster_name="cluster",
        namespace="default",
        pod_name="missing-pod",
    )
    assert result["status"] == "not_found"
    assert result["status_code"] == 404


def test_propagates_error_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_session(monkeypatch, _FakeResponse(500, text="kube-apiserver exploded"))
    result = obs.k8s_pod_delete(
        credential=SimpleNamespace(),  # type: ignore[arg-type]
        subscription_id="sub",
        resource_group="rg",
        cluster_name="cluster",
        namespace="default",
        pod_name="some-pod",
    )
    assert result["status"] == "error"
    assert result["status_code"] == 500
    assert "kube-apiserver exploded" in result["detail"]
