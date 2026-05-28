"""Tests for the PLS transition guard helpers in the openapi deploy task.

Responsibility: Cover the two helpers used by the deploy task's PLS guard —
    ``_read_service_annotations`` and ``_delete_openapi_service`` — so a
    refactor that breaks the 404 / 200 / network-error matrix is caught
    before it ships. The full ``deploy_openapi_service`` task carries too
    much external surface to test directly here; the helpers below are the
    load-bearing pieces of the guard logic and the rest is straight-line
    branching.
Edit boundaries: Patch only ``api.services.k8s.monitoring._get_k8s_session``
    so the kubeconfig / token resolution path stays unmocked. No live AKS
    calls.
Key entry points: ``test_read_service_annotations_returns_none_on_404``,
    ``test_read_service_annotations_returns_dict_on_200``,
    ``test_delete_openapi_service_raises_on_unexpected_status``,
    ``test_delete_openapi_service_accepts_404_idempotent``.
Risky contracts: ``_get_k8s_session`` returns ``(session, server)`` and the
    helper calls ``session.close()`` in ``finally`` — the fake must support
    ``.close()`` so the test does not leak a real connection.
Validation: ``uv run pytest -q api/tests/test_openapi_pls_deploy_guard.py``.
"""

from __future__ import annotations

from typing import Any

import pytest
from api.tasks.openapi import deploy as openapi_deploy


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict[str, Any] | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.content = b"{}" if payload is not None else b""

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeSession:
    def __init__(self, get_response: _FakeResponse | None = None,
                 delete_response: _FakeResponse | None = None,
                 raise_on: str = "") -> None:
        self._get_response = get_response
        self._delete_response = delete_response
        self._raise_on = raise_on
        self.closed = False
        self.get_calls: list[str] = []
        self.delete_calls: list[str] = []

    def get(self, url: str, timeout: int = 10) -> _FakeResponse:
        self.get_calls.append(url)
        if self._raise_on == "get":
            raise RuntimeError("simulated transport error")
        assert self._get_response is not None
        return self._get_response

    def delete(self, url: str, timeout: int = 15) -> _FakeResponse:
        self.delete_calls.append(url)
        if self._raise_on == "delete":
            raise RuntimeError("simulated transport error")
        assert self._delete_response is not None
        return self._delete_response

    def close(self) -> None:
        self.closed = True


def _patch_session(monkeypatch: pytest.MonkeyPatch, session: _FakeSession) -> None:
    def fake_get_session(_cred, _sub, _rg, _cluster):
        return session, "https://elb-cluster.example.k8s"

    monkeypatch.setattr(
        "api.services.k8s.monitoring._get_k8s_session", fake_get_session
    )


def test_read_service_annotations_returns_none_on_404(monkeypatch) -> None:
    """No Service yet → helper must return None so the caller treats this
    as a fresh deploy and just applies the manifest as-is."""
    session = _FakeSession(get_response=_FakeResponse(404))
    _patch_session(monkeypatch, session)

    result = openapi_deploy._read_service_annotations(
        cred=object(),
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="elb-cluster",
    )

    assert result is None
    assert session.closed is True
    assert "/services/elb-openapi" in session.get_calls[0]


def test_read_service_annotations_returns_dict_on_200(monkeypatch) -> None:
    """Existing Service → helper returns the annotation map coerced to str."""
    payload = {
        "metadata": {
            "annotations": {
                "service.beta.kubernetes.io/azure-load-balancer-internal": "true",
            }
        }
    }
    session = _FakeSession(get_response=_FakeResponse(200, payload))
    _patch_session(monkeypatch, session)

    result = openapi_deploy._read_service_annotations(
        cred=object(),
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="elb-cluster",
    )

    assert result is not None
    assert (
        result["service.beta.kubernetes.io/azure-load-balancer-internal"] == "true"
    )
    assert "service.beta.kubernetes.io/azure-pls-create" not in result
    assert session.closed is True


def test_read_service_annotations_returns_none_on_transport_error(
    monkeypatch,
) -> None:
    """Transport error → swallowed and treated as 'unknown', not raised.

    The guard must fail-open here: a one-off control-plane hiccup
    shouldn't block a deploy that would otherwise have applied
    in-place. The deploy task will still log a warning so operators
    have a breadcrumb.
    """
    session = _FakeSession(get_response=None, raise_on="get")
    _patch_session(monkeypatch, session)

    result = openapi_deploy._read_service_annotations(
        cred=object(),
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="elb-cluster",
    )

    assert result is None
    assert session.closed is True


def test_delete_openapi_service_accepts_404_idempotent(monkeypatch) -> None:
    """404 on delete is fine — Service was already gone."""
    session = _FakeSession(delete_response=_FakeResponse(404))
    _patch_session(monkeypatch, session)

    # Should NOT raise.
    openapi_deploy._delete_openapi_service(
        cred=object(),
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="elb-cluster",
    )
    assert session.closed is True
    assert "/services/elb-openapi" in session.delete_calls[0]


def test_delete_openapi_service_raises_on_unexpected_status(monkeypatch) -> None:
    """403 / 500 must surface as RuntimeError so the deploy task returns
    a structured ``openapi_pls_recreate_failed`` payload instead of a
    silent success."""
    session = _FakeSession(delete_response=_FakeResponse(500))
    _patch_session(monkeypatch, session)

    with pytest.raises(RuntimeError, match="status=500"):
        openapi_deploy._delete_openapi_service(
            cred=object(),
            subscription_id="sub-1",
            resource_group="rg-elb",
            cluster_name="elb-cluster",
        )
    assert session.closed is True
