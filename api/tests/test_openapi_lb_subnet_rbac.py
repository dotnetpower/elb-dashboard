"""Tests for the openapi LoadBalancer subnet-RBAC recovery (helper + route).

Responsibility: Lock the BYO-subnet RBAC recovery contract introduced for
    GitHub issue #33 — ``ensure_openapi_lb_subnet_rbac`` resolves the cluster
    control-plane identity + node subnet and idempotently grants Network
    Contributor, and ``POST /api/aks/openapi/lb-subnet-rbac`` shapes that into
    the synchronous response the SPA/operator consumes.
Edit boundaries: Pure unit tests with a fake ManagedCluster + stubbed grant.
    No live Azure SDK, no role-assignment write, no broker.
Key entry points: see per-test docstrings.
Risky contracts: Managed-VNet clusters MUST skip (no grant); the granted
    response MUST carry the token-cache ``note`` so the operator does not read
    the propagation delay as a failure.
Validation: ``uv run pytest -q api/tests/test_openapi_lb_subnet_rbac.py``.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class _Identity:
    def __init__(
        self, principal_id: str | None = None, user_assigned: dict[str, Any] | None = None
    ) -> None:
        self.principal_id = principal_id
        self.user_assigned_identities = user_assigned


class _Pool:
    def __init__(self, vnet_subnet_id: str | None = None) -> None:
        self.vnet_subnet_id = vnet_subnet_id


class _Cluster:
    def __init__(self, identity: Any, pools: list[_Pool]) -> None:
        self.identity = identity
        self.agent_pool_profiles = pools


class _FakeManagedClusters:
    def __init__(self, cluster: _Cluster) -> None:
        self._cluster = cluster

    def get(self, _rg: str, _name: str) -> _Cluster:
        return self._cluster


class _FakeAks:
    def __init__(self, cluster: _Cluster) -> None:
        self.managed_clusters = _FakeManagedClusters(cluster)


_SUBNET = (
    "/subscriptions/sub-1/resourceGroups/rg-elb-dashboard/providers/"
    "Microsoft.Network/virtualNetworks/vnet-elb-dashboard/subnets/snet-aks"
)


def _install(monkeypatch: pytest.MonkeyPatch, cluster: _Cluster) -> list[dict[str, Any]]:
    """Stub aks_client + the grant helper; return the list of grant calls."""
    monkeypatch.setattr(
        "api.services.azure_clients.aks_client",
        lambda _cred, _sub: _FakeAks(cluster),
    )
    grants: list[dict[str, Any]] = []

    def _fake_grant(
        _cred: Any, _sub: str, *, principal_id: str, subnet_id: str, label: str
    ) -> None:
        grants.append(
            {"principal_id": principal_id, "subnet_id": subnet_id, "label": label}
        )

    monkeypatch.setattr(
        "api.tasks.azure._grant_network_contributor_on_subnet", _fake_grant
    )
    return grants


# --------------------------------------------------------------------------- #
# Helper — ensure_openapi_lb_subnet_rbac
# --------------------------------------------------------------------------- #


def test_helper_grants_for_byo_subnet_system_assigned(monkeypatch: pytest.MonkeyPatch) -> None:
    """SystemAssigned identity + a BYO node subnet → grant on that subnet,
    status ``granted`` with the token-cache note."""
    from api.services.aks.openapi_lb_rbac import ensure_openapi_lb_subnet_rbac

    cluster = _Cluster(_Identity(principal_id="prin-123"), [_Pool(_SUBNET)])
    grants = _install(monkeypatch, cluster)

    out = ensure_openapi_lb_subnet_rbac(object(), "sub-1", "rg-elb-cluster", "elb-cluster-01")

    assert out["status"] == "granted"
    assert out["principal_id"] == "prin-123"
    assert out["subnet_id"] == _SUBNET
    assert out["role"] == "Network Contributor"
    assert "stop and start" in out["note"].lower()
    assert grants == [
        {
            "principal_id": "prin-123",
            "subnet_id": _SUBNET,
            "label": "elb-cluster-01 cluster identity (openapi LB recovery)",
        }
    ]


def test_helper_resolves_user_assigned_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """A UserAssigned control-plane identity (no top-level principal_id) is
    resolved from the first user_assigned_identities entry."""
    from api.services.aks.openapi_lb_rbac import ensure_openapi_lb_subnet_rbac

    uami = {"/subscriptions/.../uami-a": _Identity(principal_id="uami-prin")}
    cluster = _Cluster(
        _Identity(principal_id=None, user_assigned=uami), [_Pool(_SUBNET)]
    )
    grants = _install(monkeypatch, cluster)

    out = ensure_openapi_lb_subnet_rbac(object(), "sub-1", "rg", "c1")

    assert out["status"] == "granted"
    assert out["principal_id"] == "uami-prin"
    assert grants[0]["principal_id"] == "uami-prin"


def test_helper_skips_managed_vnet(monkeypatch: pytest.MonkeyPatch) -> None:
    """No agent-pool vnet_subnet_id → managed VNet → skip, no grant."""
    from api.services.aks.openapi_lb_rbac import ensure_openapi_lb_subnet_rbac

    cluster = _Cluster(_Identity(principal_id="prin-123"), [_Pool(None)])
    grants = _install(monkeypatch, cluster)

    out = ensure_openapi_lb_subnet_rbac(object(), "sub-1", "rg", "c1")

    assert out == {"status": "skipped", "reason": "managed_vnet_mode"}
    assert grants == []


def test_helper_skips_when_identity_unresolved(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cluster with no managed identity (service-principal mode) → skip."""
    from api.services.aks.openapi_lb_rbac import ensure_openapi_lb_subnet_rbac

    cluster = _Cluster(None, [_Pool(_SUBNET)])
    grants = _install(monkeypatch, cluster)

    out = ensure_openapi_lb_subnet_rbac(object(), "sub-1", "rg", "c1")

    assert out == {"status": "skipped", "reason": "cluster_identity_unresolved"}
    assert grants == []


def test_helper_is_idempotent_on_repeat(monkeypatch: pytest.MonkeyPatch) -> None:
    """Calling twice is safe (the underlying grant absorbs RoleAssignmentExists);
    here we assert the helper itself re-issues the same grant without raising."""
    from api.services.aks.openapi_lb_rbac import ensure_openapi_lb_subnet_rbac

    cluster = _Cluster(_Identity(principal_id="prin-123"), [_Pool(_SUBNET)])
    grants = _install(monkeypatch, cluster)

    ensure_openapi_lb_subnet_rbac(object(), "sub-1", "rg", "c1")
    ensure_openapi_lb_subnet_rbac(object(), "sub-1", "rg", "c1")

    assert len(grants) == 2
    assert {g["subnet_id"] for g in grants} == {_SUBNET}


# --------------------------------------------------------------------------- #
# Route — POST /api/aks/openapi/lb-subnet-rbac
# --------------------------------------------------------------------------- #


def _client_with_route(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from api.auth import CallerIdentity, require_caller
    from api.routes.aks import openapi as openapi_route

    app = FastAPI()
    app.include_router(openapi_route.router, prefix="/api/aks")
    app.dependency_overrides[require_caller] = lambda: CallerIdentity(
        object_id="caller-1",
        tenant_id="tenant-1",
        upn="alice@example.com",
        raw_token="",
        claims={},
    )
    return TestClient(app)


def test_route_rejects_missing_parameters(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client_with_route(monkeypatch)
    resp = client.post("/api/aks/openapi/lb-subnet-rbac", json={"cluster_name": "x"})
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "missing_parameters"


def test_route_delegates_to_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def _fake_ensure(
        _cred: Any, subscription_id: str, resource_group: str, cluster_name: str
    ) -> dict[str, Any]:
        captured.update(
            {
                "subscription_id": subscription_id,
                "resource_group": resource_group,
                "cluster_name": cluster_name,
            }
        )
        return {"status": "granted", "subnet_id": _SUBNET, "note": "n"}

    monkeypatch.setattr(
        "api.services.aks.openapi_lb_rbac.ensure_openapi_lb_subnet_rbac", _fake_ensure
    )
    monkeypatch.setattr("api.services.get_credential", lambda: object())

    client = _client_with_route(monkeypatch)
    resp = client.post(
        "/api/aks/openapi/lb-subnet-rbac",
        json={
            "subscription_id": "sub-1",
            "resource_group": "rg-elb-cluster",
            "cluster_name": "elb-cluster-01",
        },
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "granted"
    assert captured == {
        "subscription_id": "sub-1",
        "resource_group": "rg-elb-cluster",
        "cluster_name": "elb-cluster-01",
    }


def test_route_returns_502_when_helper_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a: Any, **_k: Any) -> dict[str, Any]:
        raise RuntimeError("ARM down")

    monkeypatch.setattr(
        "api.services.aks.openapi_lb_rbac.ensure_openapi_lb_subnet_rbac", _boom
    )
    monkeypatch.setattr("api.services.get_credential", lambda: object())

    client = _client_with_route(monkeypatch)
    resp = client.post(
        "/api/aks/openapi/lb-subnet-rbac",
        json={
            "subscription_id": "sub-1",
            "resource_group": "rg-elb-cluster",
            "cluster_name": "elb-cluster-01",
        },
    )

    assert resp.status_code == 502
    assert resp.json()["detail"]["code"] == "lb_subnet_rbac_grant_failed"
