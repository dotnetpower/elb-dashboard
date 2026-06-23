"""Unit tests for the IP-based OpenAPI runtime endpoint re-stamp reconciler.

Covers the Service Bus drain-deadlock fix: the beat task must no-op when the
integration is off or no cluster context is resolvable, re-stamp the durable
endpoint when the live Service IP resolves (from the SB config OR the durable
metadata fallback), and leave a Stopped cluster's endpoint to age out.

Responsibility: One behaviour family — the reconcile decision matrix.
Edit boundaries: Test-only.
Key entry points: ``test_*``.
Risky contracts: Mirrors the lazy-import sites in
    ``api.tasks.openapi.reconcile_runtime_endpoint``.
Validation: ``uv run pytest -q api/tests/test_openapi_runtime_endpoint_reconcile.py``.
"""

from __future__ import annotations

from typing import Any

import pytest
from api.services import service_bus_pref
from api.services.openapi import runtime as openapi_runtime
from api.services.service_bus_pref import ServiceBusConfig
from api.tasks.openapi import reconcile_runtime_endpoint as mod


def _patch_save(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    saved: list[dict[str, Any]] = []

    def _fake_save(base_url: str, *, metadata: dict[str, Any] | None = None, **_kw: Any) -> bool:
        saved.append({"base_url": base_url, "metadata": metadata or {}})
        return True

    monkeypatch.setattr(openapi_runtime, "save_openapi_base_url", _fake_save)
    return saved


def _run() -> dict[str, Any]:
    return mod.reconcile_openapi_runtime_endpoint.run()


def test_skips_when_servicebus_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service_bus_pref, "service_bus_enabled", lambda: False)
    saved = _patch_save(monkeypatch)
    result = _run()
    assert result["reason"] == "servicebus_disabled"
    assert saved == []


def test_skips_when_no_cluster_context(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service_bus_pref, "service_bus_enabled", lambda: True)
    monkeypatch.setattr(service_bus_pref, "get_service_bus_config", lambda: ServiceBusConfig())
    monkeypatch.setattr(openapi_runtime, "get_openapi_runtime_metadata", lambda: {})
    saved = _patch_save(monkeypatch)
    result = _run()
    assert result["reason"] == "no_cluster_context"
    assert saved == []


def test_restamps_from_sb_config_when_ip_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service_bus_pref, "service_bus_enabled", lambda: True)
    monkeypatch.setattr(
        service_bus_pref,
        "get_service_bus_config",
        lambda: ServiceBusConfig(
            subscription_id="sub-1", resource_group="rg-1", cluster_name="elb-cluster"
        ),
    )
    from api.services.k8s import monitoring

    monkeypatch.setattr(monitoring, "k8s_get_service_ip", lambda *_a, **_kw: "10.20.4.15")
    saved = _patch_save(monkeypatch)
    result = _run()
    assert result["status"] == "reconciled"
    assert result["cluster_name"] == "elb-cluster"
    assert len(saved) == 1
    assert saved[0]["base_url"] == "http://10.20.4.15"
    assert saved[0]["metadata"]["cluster_name"] == "elb-cluster"


def test_falls_back_to_durable_metadata_when_config_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(service_bus_pref, "service_bus_enabled", lambda: True)
    monkeypatch.setattr(service_bus_pref, "get_service_bus_config", lambda: ServiceBusConfig())
    monkeypatch.setattr(
        openapi_runtime,
        "get_openapi_runtime_metadata",
        lambda: {
            "subscription_id": "sub-meta",
            "resource_group": "rg-meta",
            "cluster_name": "cluster-meta",
        },
    )
    from api.services.k8s import monitoring

    monkeypatch.setattr(monitoring, "k8s_get_service_ip", lambda *_a, **_kw: "10.0.0.9")
    saved = _patch_save(monkeypatch)
    result = _run()
    assert result["status"] == "reconciled"
    assert result["cluster_name"] == "cluster-meta"
    assert saved[0]["base_url"] == "http://10.0.0.9"


def test_skips_restamp_when_cluster_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service_bus_pref, "service_bus_enabled", lambda: True)
    monkeypatch.setattr(
        service_bus_pref,
        "get_service_bus_config",
        lambda: ServiceBusConfig(
            subscription_id="sub-1", resource_group="rg-1", cluster_name="elb-cluster"
        ),
    )
    from api.services.k8s import monitoring

    # Stopped cluster: the live Service IP does not resolve.
    monkeypatch.setattr(monitoring, "k8s_get_service_ip", lambda *_a, **_kw: None)
    saved = _patch_save(monkeypatch)
    result = _run()
    assert result["reason"] == "service_ip_unresolved"
    assert saved == []
