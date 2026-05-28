"""Tests for the `/api/settings/vnet-peering` settings route.

Responsibility: Cover input validation and the synchronous summary return
shape exposed to the Settings panel.
Edit boundaries: HTTP shaping only. Azure work is stubbed out.
Key entry points: `peer_vnet`.
Risky contracts: The helper returns best-effort payloads with `error` on
partial failures; the route forwards those values instead of bubbling the
Azure exception to the UI.
Validation: `uv run pytest -q api/tests/test_settings_vnet_peering.py`.
"""

from __future__ import annotations

from typing import Any

from api.auth import CallerIdentity, require_caller
from fastapi.testclient import TestClient


def _build_app(monkeypatch):
    from api.routes.settings import vnet_peering as settings_route
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(settings_route.router)
    app.dependency_overrides[require_caller] = lambda: CallerIdentity(
        object_id="caller-1",
        tenant_id="tenant-1",
        upn="alice@example.com",
        raw_token="",
        claims={},
    )
    return app


def test_route_rejects_missing_parameters(monkeypatch) -> None:
    app = _build_app(monkeypatch)
    client = TestClient(app)

    resp = client.post("/vnet-peering", json={"subscription_id": "x"})

    assert resp.status_code == 400


def test_route_returns_helper_summary(monkeypatch) -> None:
    app = _build_app(monkeypatch)
    client = TestClient(app)

    def _fake_ensure(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "target_vnet": (
                "/subscriptions/sub-2/resourceGroups/rg-target/"
                "providers/Microsoft.Network/virtualNetworks/vnet-target"
            ),
            "peerings": [
                {
                    "direction": "target_to_aks",
                    "name": "peer-target-to-aks",
                    "state": "Connected",
                },
                {
                    "direction": "aks_to_target",
                    "name": "peer-aks-to-target",
                    "state": "Connected",
                },
            ],
            "probe": {
                "target_ip": "10.224.0.7",
                "reachable": True,
                "status_code": 200,
                "latency_ms": 10.0,
                "message": "OK",
            },
        }

    monkeypatch.setattr("api.tasks.azure.peering.ensure_vnet_peering_with_target", _fake_ensure)
    monkeypatch.setattr("api.services.get_credential", lambda: object())

    resp = client.post(
        "/vnet-peering",
        json={
            "subscription_id": "00000000-0000-0000-0000-000000000001",
            "resource_group": "rg-workload",
            "cluster_name": "elb-cluster-01",
            "target_subscription_id": "00000000-0000-0000-0000-000000000002",
            "target_resource_group": "rg-target",
            "target_vnet_name": "vnet-target",
        },
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["probe"]["reachable"] is True
    assert {p["direction"] for p in resp.json()["peerings"]} == {
        "target_to_aks",
        "aks_to_target",
    }


def test_route_returns_502_when_helper_raises(monkeypatch) -> None:
    app = _build_app(monkeypatch)
    client = TestClient(app)

    def _boom(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("ARM down")

    monkeypatch.setattr("api.tasks.azure.peering.ensure_vnet_peering_with_target", _boom)
    monkeypatch.setattr("api.services.get_credential", lambda: object())

    resp = client.post(
        "/vnet-peering",
        json={
            "subscription_id": "00000000-0000-0000-0000-000000000001",
            "resource_group": "rg-workload",
            "cluster_name": "elb-cluster-01",
            "target_subscription_id": "00000000-0000-0000-0000-000000000002",
            "target_resource_group": "rg-target",
            "target_vnet_name": "vnet-target",
        },
    )

    assert resp.status_code == 502
    assert resp.json()["detail"]["code"] == "vnet_peering_unavailable"
