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

from typing import Any, ClassVar

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


def test_helper_reuses_passed_cluster_without_arm_get(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the caller (deploy_openapi_service) passes a pre-fetched cluster,
    the helper must NOT call aks_client — avoiding a duplicate ARM read."""
    from api.services.aks.openapi_lb_rbac import ensure_openapi_lb_subnet_rbac

    grants: list[dict[str, Any]] = []

    def _fake_grant(
        _cred: Any, _sub: str, *, principal_id: str, subnet_id: str, label: str
    ) -> None:
        grants.append({"principal_id": principal_id, "subnet_id": subnet_id})

    monkeypatch.setattr(
        "api.tasks.azure._grant_network_contributor_on_subnet", _fake_grant
    )

    def _boom_aks(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("aks_client must not be called when cluster is passed")

    monkeypatch.setattr("api.services.azure_clients.aks_client", _boom_aks)

    cluster = _Cluster(_Identity(principal_id="prin-passed"), [_Pool(_SUBNET)])
    out = ensure_openapi_lb_subnet_rbac(
        object(), "sub-1", "rg", "c1", cluster=cluster
    )

    assert out["status"] == "granted"
    assert out["principal_id"] == "prin-passed"
    assert grants == [{"principal_id": "prin-passed", "subnet_id": _SUBNET}]



# --------------------------------------------------------------------------- #
# Detection — detect_lb_subnet_rbac_missing
# --------------------------------------------------------------------------- #


def _patch_events(monkeypatch: pytest.MonkeyPatch, events: list[dict[str, Any]]) -> None:
    monkeypatch.setattr(
        "api.services.k8s.observability.k8s_list_events",
        lambda *a, **k: events,
    )


def test_detect_true_on_subnet_403_event(monkeypatch: pytest.MonkeyPatch) -> None:
    """A SyncLoadBalancerFailed event on the elb-openapi Service whose message
    is a subnet AuthorizationFailed is the #33 signature → True."""
    from api.services.aks.openapi_lb_rbac import detect_lb_subnet_rbac_missing

    _patch_events(
        monkeypatch,
        [
            {
                "involved_name": "elb-openapi",
                "reason": "SyncLoadBalancerFailed",
                "message": (
                    "Error syncing load balancer: failed to ensure load balancer: "
                    "GET .../subnets/snet-aks RESPONSE 403: AuthorizationFailed"
                ),
            }
        ],
    )
    assert detect_lb_subnet_rbac_missing(object(), "s", "rg", "c1") is True


def test_detect_false_on_unrelated_event(monkeypatch: pytest.MonkeyPatch) -> None:
    """An LB failure that is not a subnet-auth problem (e.g. quota) must not be
    misclassified as the RBAC gap."""
    from api.services.aks.openapi_lb_rbac import detect_lb_subnet_rbac_missing

    _patch_events(
        monkeypatch,
        [
            {
                "involved_name": "elb-openapi",
                "reason": "SyncLoadBalancerFailed",
                "message": "Error syncing load balancer: quota exceeded for public IPs",
            }
        ],
    )
    assert detect_lb_subnet_rbac_missing(object(), "s", "rg", "c1") is False


def test_detect_false_for_other_service(monkeypatch: pytest.MonkeyPatch) -> None:
    """A subnet-403 event on a DIFFERENT Service must not match."""
    from api.services.aks.openapi_lb_rbac import detect_lb_subnet_rbac_missing

    _patch_events(
        monkeypatch,
        [
            {
                "involved_name": "some-other-svc",
                "reason": "SyncLoadBalancerFailed",
                "message": "subnets/snet-x 403 AuthorizationFailed",
            }
        ],
    )
    assert detect_lb_subnet_rbac_missing(object(), "s", "rg", "c1") is False


def test_detect_false_on_events_read_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A read failure degrades to False (caller falls back to the generic hint)."""
    from api.services.aks.openapi_lb_rbac import detect_lb_subnet_rbac_missing

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("k8s unreachable")

    monkeypatch.setattr("api.services.k8s.observability.k8s_list_events", _boom)
    assert detect_lb_subnet_rbac_missing(object(), "s", "rg", "c1") is False


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


# --------------------------------------------------------------------------- #
# deploy_openapi_service integration — grant runs BEFORE the Service is applied
# --------------------------------------------------------------------------- #


def _stub_deploy_prelude(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Stub the deploy task up to the grant, then hard-stop at pls_config_from_env.

    Returns the list of recorded grant calls. The task short-circuits with a
    PLS misconfig error immediately AFTER the grant step, so the test isolates
    the grant integration without standing up kubectl / k8s probes.
    """
    from api.tasks.openapi import deploy as deploy_mod

    class _Region:
        location = "koreacentral"
        agent_pool_profiles: ClassVar[list[Any]] = []

    class _ManagedClusters:
        @staticmethod
        def get(_rg: str, _name: str) -> _Region:
            return _Region()

    class _Aks:
        managed_clusters = _ManagedClusters()

    monkeypatch.setattr(deploy_mod, "get_credential", lambda: object())
    monkeypatch.setattr(deploy_mod, "aks_client", lambda _c, _s: _Aks())
    monkeypatch.setattr(
        deploy_mod,
        "setup_workload_identity",
        lambda *a, **k: {"mi_client_id": "mi-x"},
    )
    monkeypatch.setattr(
        "api.services.openapi.runtime.get_openapi_api_token",
        lambda **k: "tok",
    )
    # Stop the task right after the grant step so we never reach kubectl apply.
    def _pls_boom() -> Any:
        raise ValueError("stop-after-grant")

    monkeypatch.setattr(deploy_mod, "pls_config_from_env", _pls_boom)

    grants: list[dict[str, Any]] = []

    def _record(
        _cred: Any,
        subscription_id: str,
        resource_group: str,
        cluster_name: str,
        *,
        cluster: Any = None,
    ) -> dict[str, Any]:
        grants.append(
            {"cluster_name": cluster_name, "cluster_passed": cluster is not None}
        )
        return {"status": "granted"}

    monkeypatch.setattr(
        "api.services.aks.openapi_lb_rbac.ensure_openapi_lb_subnet_rbac", _record
    )
    return grants


def test_deploy_grants_lb_subnet_rbac_before_apply(monkeypatch: pytest.MonkeyPatch) -> None:
    """deploy_openapi_service must grant the node-subnet RBAC (reusing the
    already-fetched cluster) before it applies the Service manifest."""
    from api.tasks.openapi import deploy as deploy_mod

    grants = _stub_deploy_prelude(monkeypatch)

    out = deploy_mod.deploy_openapi_service.run(
        subscription_id="sub-1",
        resource_group="rg-elb-cluster",
        cluster_name="elb-cluster-01",
        acr_name="",  # avoid the acr_resource_group requirement
    )

    # The grant ran, was passed the pre-fetched cluster, and the task then
    # short-circuited at the PLS step (proving the grant precedes apply).
    assert grants == [{"cluster_name": "elb-cluster-01", "cluster_passed": True}]
    assert out["status"] == "failed"
    assert out["openapi_deploy"]["code"] == "openapi_pls_misconfigured"


def test_deploy_tolerates_lb_subnet_rbac_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A grant failure is best-effort — it must not abort the deploy. The task
    proceeds to the next step (here the PLS short-circuit) regardless."""
    from api.tasks.openapi import deploy as deploy_mod

    _stub_deploy_prelude(monkeypatch)

    def _boom(*_a: Any, **_k: Any) -> dict[str, Any]:
        raise RuntimeError("subnet grant ARM 403")

    monkeypatch.setattr(
        "api.services.aks.openapi_lb_rbac.ensure_openapi_lb_subnet_rbac", _boom
    )

    out = deploy_mod.deploy_openapi_service.run(
        subscription_id="sub-1",
        resource_group="rg-elb-cluster",
        cluster_name="elb-cluster-01",
        acr_name="",
    )

    # Grant raised, but the task continued (reached the PLS step) instead of
    # surfacing the grant error.
    assert out["status"] == "failed"
    assert out["openapi_deploy"]["code"] == "openapi_pls_misconfigured"



# --------------------------------------------------------------------------- #
# spec route — degraded payload picks the specific RBAC hint when detected
# --------------------------------------------------------------------------- #


def _spec_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setenv("API_CLIENT_ID", "00000000-0000-0000-0000-000000000000")
    monkeypatch.delenv("OPENAPI_PUBLIC_BASE_URL", raising=False)
    # No public TLS base, and the LB IP is missing → the route takes the
    # "not reachable" branch where the recovery hint is chosen.
    monkeypatch.setattr(
        "api.services.openapi.runtime.get_public_tls_base_url", lambda **k: ""
    )
    monkeypatch.setattr(
        "api.services.k8s.monitoring.k8s_get_service_ip", lambda *a, **k: None
    )
    from api.main import app

    return TestClient(app)


def test_spec_returns_rbac_hint_when_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the LB-pending cause is the subnet-RBAC gap, the degraded spec
    payload carries recovery_action=grant_lb_subnet_rbac (not the peering hint)."""
    monkeypatch.setattr(
        "api.services.aks.openapi_lb_rbac.detect_lb_subnet_rbac_missing",
        lambda *a, **k: True,
    )
    client = _spec_client(monkeypatch)
    r = client.get(
        "/api/aks/openapi/spec",
        params={"resource_group": "rg-elb-cluster", "cluster_name": "elb-cluster-01"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["degraded"] is True
    assert body["recovery_action"] == "grant_lb_subnet_rbac"


def test_spec_falls_back_to_peering_hint_when_not_rbac(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the subnet-RBAC signature is absent, the route keeps the generic
    peering recovery hint (backward compatible)."""
    monkeypatch.setattr(
        "api.services.aks.openapi_lb_rbac.detect_lb_subnet_rbac_missing",
        lambda *a, **k: False,
    )
    client = _spec_client(monkeypatch)
    r = client.get(
        "/api/aks/openapi/spec",
        params={"resource_group": "rg-elb-cluster", "cluster_name": "elb-cluster-01"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["degraded"] is True
    assert body["recovery_action"] == "peer_with_platform"
