"""Tests for ``api.services.openapi.pls_status.get_pls_status``.

Validates the read-only PLS state probe used by the ``/aks/openapi/pls``
route. Each test pins ``api.services.k8s.monitoring._get_k8s_session`` and
``api.services.openapi.pls_status.pls_config_from_env`` via monkeypatch so
nothing reaches a real cluster.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from api.services.openapi import pls_status as mod
from api.tasks.openapi.constants import PlsConfig


class _FakeSession:
    def __init__(self, status: int, body: dict[str, Any] | None = None) -> None:
        self.status = status
        self.body = body or {}
        self.closed = False

    def get(self, _url: str, timeout: int | float = 10) -> Any:
        del timeout
        return SimpleNamespace(
            status_code=self.status,
            json=lambda: self.body,
        )

    def close(self) -> None:
        self.closed = True


def _pin_session(monkeypatch: pytest.MonkeyPatch, session: _FakeSession) -> None:
    monkeypatch.setattr(
        "api.services.k8s.monitoring._get_k8s_session",
        lambda *_a, **_k: (session, "https://kube"),
    )


def _pin_cfg(
    monkeypatch: pytest.MonkeyPatch, *, enabled: bool, subnet: str = "snet-elb-lb"
) -> None:
    monkeypatch.setattr(
        mod,
        "pls_config_from_env",
        lambda: PlsConfig(
            enabled=enabled,
            name="pls-elb-openapi",
            lb_subnet=subnet,
            visibility="*",
            auto_approval="",
        ),
    )


def test_pls_status_service_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _pin_cfg(monkeypatch, enabled=True)
    fake = _FakeSession(status=404)
    _pin_session(monkeypatch, fake)
    out = mod.get_pls_status(
        cred=None,
        subscription_id="sub",
        resource_group="rg",
        cluster_name="elb-cluster",
    )
    assert out.available is True
    assert out.service_exists is False
    assert out.transition_pending is False
    assert out.confirm_recreate_required is False
    assert fake.closed is True


def test_pls_status_service_already_has_annotation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pin_cfg(monkeypatch, enabled=True)
    fake = _FakeSession(
        status=200,
        body={
            "metadata": {
                "annotations": {
                    "service.beta.kubernetes.io/azure-pls-create": "true",
                }
            }
        },
    )
    _pin_session(monkeypatch, fake)
    out = mod.get_pls_status(
        cred=None,
        subscription_id="sub",
        resource_group="rg",
        cluster_name="elb-cluster",
    )
    assert out.available is True
    assert out.service_has_pls_annotation is True
    assert out.transition_pending is False
    assert out.confirm_recreate_required is False


def test_pls_status_transition_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    _pin_cfg(monkeypatch, enabled=True)
    fake = _FakeSession(
        status=200,
        body={"metadata": {"annotations": {"unrelated": "x"}}},
    )
    _pin_session(monkeypatch, fake)
    out = mod.get_pls_status(
        cred=None,
        subscription_id="sub",
        resource_group="rg",
        cluster_name="elb-cluster",
    )
    assert out.available is True
    assert out.service_exists is True
    assert out.service_has_pls_annotation is False
    assert out.transition_pending is True
    assert out.confirm_recreate_required is True


def test_pls_status_pls_disabled_in_env_never_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pin_cfg(monkeypatch, enabled=False, subnet="")
    fake = _FakeSession(
        status=200,
        body={"metadata": {"annotations": {}}},
    )
    _pin_session(monkeypatch, fake)
    out = mod.get_pls_status(
        cred=None,
        subscription_id="sub",
        resource_group="rg",
        cluster_name="elb-cluster",
    )
    assert out.available is True
    assert out.pls_enabled_env is False
    assert out.transition_pending is False


def test_pls_status_unexpected_status_degrades(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pin_cfg(monkeypatch, enabled=True)
    fake = _FakeSession(status=500)
    _pin_session(monkeypatch, fake)
    out = mod.get_pls_status(
        cred=None,
        subscription_id="sub",
        resource_group="rg",
        cluster_name="elb-cluster",
    )
    assert out.available is False
    assert out.reason == "k8s_unexpected_status"
    assert fake.closed is True


def test_pls_status_probe_exception_degrades(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pin_cfg(monkeypatch, enabled=True)

    class _ErrSession:
        def __init__(self) -> None:
            self.closed = False

        def get(self, *_a: Any, **_k: Any) -> Any:
            raise RuntimeError("network down")

        def close(self) -> None:
            self.closed = True

    err = _ErrSession()
    monkeypatch.setattr(
        "api.services.k8s.monitoring._get_k8s_session",
        lambda *_a, **_k: (err, "https://kube"),
    )
    out = mod.get_pls_status(
        cred=None,
        subscription_id="sub",
        resource_group="rg",
        cluster_name="elb-cluster",
    )
    assert out.available is False
    assert out.reason == "k8s_probe_failed"
    assert err.closed is True


def test_pls_status_to_dict_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    _pin_cfg(monkeypatch, enabled=True)
    fake = _FakeSession(
        status=200,
        body={"metadata": {"annotations": {}}},
    )
    _pin_session(monkeypatch, fake)
    out = mod.get_pls_status(
        cred=None,
        subscription_id="sub",
        resource_group="rg",
        cluster_name="elb-cluster",
    ).to_dict()
    # Stable contract for the SPA.
    expected_keys = {
        "available",
        "pls_enabled_env",
        "pls_name",
        "service_exists",
        "service_has_pls_annotation",
        "transition_pending",
        "confirm_recreate_required",
        "reason",
    }
    assert set(out.keys()) == expected_keys
