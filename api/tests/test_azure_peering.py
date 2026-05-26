"""Tests for the VNet peering helper used by `provision_aks` + the
`/api/aks/peer-with-platform` recovery route.

Responsibility: Cover the auto-peering flow that closes the
    "api sidecar cannot reach AKS internal LB IP" gap (2026-05-27).
Edit boundaries: Use Azure SDK fakes; never touch real Azure.
Key entry points: `test_helper_*`, `test_route_*`.
Risky contracts: Helper must NEVER raise on per-peering failure — record
    into `error` + `recovery_command` and return so `provision_aks`
    can include the summary in the completion payload without failing
    the task.
Validation: `uv run pytest -q api/tests/test_azure_peering.py`.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _clear_platform_subnet_env(monkeypatch):
    monkeypatch.delenv("PLATFORM_PRIVATE_ENDPOINT_SUBNET_ID", raising=False)
    monkeypatch.delenv("AZURE_SUBSCRIPTION_ID", raising=False)
    yield


def _make_cluster(node_rg: str = "MC_rg-elb-cluster_elb-cluster-01_koreacentral"):
    return SimpleNamespace(node_resource_group=node_rg)


def _make_resource(res_id: str):
    return SimpleNamespace(id=res_id)


def _install_clients(
    monkeypatch,
    *,
    cluster_obj=None,
    node_rg_vnets: list[str] | None = None,
    peering_recorder: list[dict[str, Any]] | None = None,
    peering_raises: dict[str, Exception] | None = None,
) -> dict[str, Any]:
    """Patch the network/resource/aks clients used by the helper.

    Returns a `state` dict the test can read after invocation
    (e.g. number of resource.list calls, peering bodies).
    """
    state: dict[str, Any] = {
        "list_calls": 0,
        "peering_recorder": peering_recorder if peering_recorder is not None else [],
        "peering_raises": peering_raises or {},
        "cluster_obj": cluster_obj or _make_cluster(),
        "node_rg_vnets": node_rg_vnets if node_rg_vnets is not None else [],
    }

    class _FakePoller:
        def __init__(self, name: str, vnet_name: str) -> None:
            self._name = name
            self._vnet_name = vnet_name

        def result(self) -> SimpleNamespace:
            return SimpleNamespace(peering_state="Connected")

    class _FakePeerings:
        def begin_create_or_update(
            self,
            rg: str,
            vnet: str,
            name: str,
            body: dict[str, Any],
        ) -> _FakePoller:
            state["peering_recorder"].append(
                {"local_rg": rg, "local_vnet": vnet, "name": name, "body": body}
            )
            if name in state["peering_raises"]:
                raise state["peering_raises"][name]
            return _FakePoller(name, vnet)

        def get(self, rg: str, vnet: str, name: str) -> SimpleNamespace:
            return SimpleNamespace(peering_state="Connected")

    class _FakeNetworkClient:
        virtual_network_peerings = _FakePeerings()

    class _FakeResources:
        def list_by_resource_group(self, rg: str, filter: str = ""):
            state["list_calls"] += 1
            state["last_list_rg"] = rg
            state["last_list_filter"] = filter
            return [_make_resource(v) for v in state["node_rg_vnets"]]

    class _FakeResourceClient:
        resources = _FakeResources()

    class _FakeAksClient:
        managed_clusters = SimpleNamespace(
            get=lambda _rg, _name: state["cluster_obj"]
        )

    from api.services import azure_clients
    from api.tasks.azure import peering as peering_mod

    monkeypatch.setattr(peering_mod, "network_client", lambda _c, _s: _FakeNetworkClient())
    monkeypatch.setattr(peering_mod, "resource_client", lambda _c, _s: _FakeResourceClient())
    monkeypatch.setattr(azure_clients, "aks_client", lambda _c, _s: _FakeAksClient())
    return state


def test_helper_skips_when_dashboard_vnet_unresolvable(monkeypatch) -> None:
    """No `PLATFORM_PRIVATE_ENDPOINT_SUBNET_ID` and no explicit arg → skipped.

    The local-dev shell hits this branch. The helper must not touch the
    Azure SDK and must still emit a `recovery_command` so the SPA can
    show it.
    """
    from api.tasks.azure.peering import ensure_vnet_peering_with_cluster

    result = ensure_vnet_peering_with_cluster(
        object(),
        subscription_id="sub-1",
        cluster_resource_group="rg-elb-cluster",
        cluster_name="elb-cluster-01",
    )

    assert result["skipped"] is True
    assert result["reason"] == "dashboard_vnet_id not resolved"
    assert "peer-cluster-network.sh" in result["recovery_command"]
    assert "--cluster-name elb-cluster-01" in result["recovery_command"]


def test_helper_skips_when_aks_node_rg_has_no_vnet(monkeypatch) -> None:
    """BYO-VNet mode: the cluster lives in the platform VNet already and
    `MC_*` RG carries no VNet to peer with. Skip with a clear reason.
    """
    _install_clients(monkeypatch, node_rg_vnets=[])
    from api.tasks.azure.peering import ensure_vnet_peering_with_cluster

    result = ensure_vnet_peering_with_cluster(
        object(),
        subscription_id="sub-1",
        cluster_resource_group="rg-elb-cluster",
        cluster_name="elb-cluster-01",
        dashboard_vnet_id=(
            "/subscriptions/sub-1/resourceGroups/rg-elb-dashboard/providers/"
            "Microsoft.Network/virtualNetworks/vnet-elb-dashboard"
        ),
    )

    assert result["skipped"] is True
    assert result["reason"] == "aks_node_rg_has_no_vnet"
    assert "node_resource_group" in result


def test_helper_creates_both_directions_on_happy_path(monkeypatch) -> None:
    """The two expected peerings (dashboard→AKS, AKS→dashboard) land
    with stable names, ServicePrincipal-only access, no gateway transit.
    """
    aks_vnet = (
        "/subscriptions/sub-1/resourceGroups/MC_rg-elb-cluster_elb-cluster-01_koreacentral/"
        "providers/Microsoft.Network/virtualNetworks/aks-vnet-23268255"
    )
    state = _install_clients(monkeypatch, node_rg_vnets=[aks_vnet])
    from api.tasks.azure.peering import ensure_vnet_peering_with_cluster

    result = ensure_vnet_peering_with_cluster(
        object(),
        subscription_id="sub-1",
        cluster_resource_group="rg-elb-cluster",
        cluster_name="elb-cluster-01",
        dashboard_vnet_id=(
            "/subscriptions/sub-1/resourceGroups/rg-elb-dashboard/providers/"
            "Microsoft.Network/virtualNetworks/vnet-elb-dashboard"
        ),
    )

    assert "error" not in result, result
    assert result["aks_vnet"] == aks_vnet
    assert result["node_resource_group"] == (
        "MC_rg-elb-cluster_elb-cluster-01_koreacentral"
    )
    peerings = result["peerings"]
    directions = sorted(p["direction"] for p in peerings)
    assert directions == ["aks_to_dashboard", "dashboard_to_aks"]
    for p in peerings:
        assert p["state"] == "Connected"

    # Names follow `peer-<local>-to-<remote>` so re-runs hit the same row.
    recorder = state["peering_recorder"]
    assert len(recorder) == 2
    names = [r["name"] for r in recorder]
    assert names[0] == "peer-vnet-elb-dashboard-to-aks-vnet-23268255"
    assert names[1] == "peer-aks-vnet-23268255-to-vnet-elb-dashboard"

    # Body: no gateway transit, RFC peering defaults that keep both sides
    # independent of each other for routing of arbitrary egress.
    for r in recorder:
        body = r["body"]
        assert body["allow_virtual_network_access"] is True
        assert body["allow_forwarded_traffic"] is False
        assert body["allow_gateway_transit"] is False
        assert body["use_remote_gateways"] is False


def test_helper_treats_already_exists_as_success(monkeypatch) -> None:
    """Re-runs against an already-peered pair land in `peerings` with
    state `Connected` (not in `error`). The shell helper relies on this
    so a manual recovery after the auto-peer step ran is a clean no-op.
    """
    aks_vnet = (
        "/subscriptions/sub-1/resourceGroups/MC_rg-elb-cluster_elb-cluster-01_koreacentral/"
        "providers/Microsoft.Network/virtualNetworks/aks-vnet-23268255"
    )

    class _Conflict(Exception):
        pass

    state = _install_clients(
        monkeypatch,
        node_rg_vnets=[aks_vnet],
        peering_raises={
            "peer-vnet-elb-dashboard-to-aks-vnet-23268255": _Conflict(
                "(AlreadyExists) The peering already exists."
            ),
            "peer-aks-vnet-23268255-to-vnet-elb-dashboard": _Conflict(
                "(Conflict) Peering exists."
            ),
        },
    )
    from api.tasks.azure.peering import ensure_vnet_peering_with_cluster

    result = ensure_vnet_peering_with_cluster(
        object(),
        subscription_id="sub-1",
        cluster_resource_group="rg-elb-cluster",
        cluster_name="elb-cluster-01",
        dashboard_vnet_id=(
            "/subscriptions/sub-1/resourceGroups/rg-elb-dashboard/providers/"
            "Microsoft.Network/virtualNetworks/vnet-elb-dashboard"
        ),
    )

    assert "error" not in result, result
    assert {p["state"] for p in result["peerings"]} == {"Connected"}
    _ = state


def test_helper_records_authorization_failure_without_raising(monkeypatch) -> None:
    """When the MI lacks Network Contributor on the AKS-auto VNet, the
    write fails with `AuthorizationFailed`. The helper must capture the
    error string into `error` (+ keep `recovery_command`) and return —
    it must NOT raise, because `provision_aks` would otherwise mark the
    whole cluster create failed.
    """
    aks_vnet = (
        "/subscriptions/sub-1/resourceGroups/MC_rg-elb-cluster_elb-cluster-01_koreacentral/"
        "providers/Microsoft.Network/virtualNetworks/aks-vnet-23268255"
    )

    class _AuthFailed(Exception):
        pass

    _install_clients(
        monkeypatch,
        node_rg_vnets=[aks_vnet],
        peering_raises={
            "peer-vnet-elb-dashboard-to-aks-vnet-23268255": _AuthFailed(
                "(AuthorizationFailed) MI lacks virtualNetworks/peer/action"
            ),
        },
    )
    from api.tasks.azure.peering import ensure_vnet_peering_with_cluster

    result = ensure_vnet_peering_with_cluster(
        object(),
        subscription_id="sub-1",
        cluster_resource_group="rg-elb-cluster",
        cluster_name="elb-cluster-01",
        dashboard_vnet_id=(
            "/subscriptions/sub-1/resourceGroups/rg-elb-dashboard/providers/"
            "Microsoft.Network/virtualNetworks/vnet-elb-dashboard"
        ),
    )

    assert "error" in result
    assert "AuthorizationFailed" in result["error"]
    assert "dashboard_to_aks" in result["error"]
    # The reverse direction still got tried so we don't silently skip half
    # the recovery on a transient failure of the first direction.
    assert any(p["direction"] == "aks_to_dashboard" for p in result["peerings"])
    assert "peer-cluster-network.sh" in result["recovery_command"]


def test_helper_reads_dashboard_vnet_from_env(monkeypatch) -> None:
    """`PLATFORM_PRIVATE_ENDPOINT_SUBNET_ID` is the canonical source in
    the Container Apps env. The helper must strip `/subnets/<name>` to
    derive the parent VNet ARM id without any Bicep change.
    """
    subnet_id = (
        "/subscriptions/sub-1/resourceGroups/rg-elb-dashboard/providers/"
        "Microsoft.Network/virtualNetworks/vnet-elb-dashboard/subnets/snet-pe"
    )
    monkeypatch.setenv("PLATFORM_PRIVATE_ENDPOINT_SUBNET_ID", subnet_id)
    aks_vnet = (
        "/subscriptions/sub-1/resourceGroups/MC_rg-elb-cluster_elb-cluster-01_koreacentral/"
        "providers/Microsoft.Network/virtualNetworks/aks-vnet-23268255"
    )
    _install_clients(monkeypatch, node_rg_vnets=[aks_vnet])
    from api.tasks.azure.peering import ensure_vnet_peering_with_cluster

    result = ensure_vnet_peering_with_cluster(
        object(),
        subscription_id="sub-1",
        cluster_resource_group="rg-elb-cluster",
        cluster_name="elb-cluster-01",
    )

    expected_dash = (
        "/subscriptions/sub-1/resourceGroups/rg-elb-dashboard/providers/"
        "Microsoft.Network/virtualNetworks/vnet-elb-dashboard"
    )
    assert result["dashboard_vnet"] == expected_dash


# ---------------------------------------------------------------------------
# Route — /api/aks/peer-with-platform
# ---------------------------------------------------------------------------


def _build_app_with_route(monkeypatch):
    from api.routes.aks import peering as peering_route
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(peering_route.router, prefix="/api/aks")
    return app, peering_route


def test_route_rejects_missing_parameters(monkeypatch) -> None:
    from api.auth import CallerIdentity, require_caller
    from fastapi.testclient import TestClient

    app, _ = _build_app_with_route(monkeypatch)
    app.dependency_overrides[require_caller] = lambda: CallerIdentity(
        object_id="caller-1",
        tenant_id="tenant-1",
        upn="alice@example.com",
        raw_token="",
        claims={},
    )

    client = TestClient(app)
    resp = client.post("/api/aks/peer-with-platform", json={"cluster_name": "x"})
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "missing_parameters"


def test_route_delegates_to_helper_and_returns_summary(monkeypatch) -> None:
    from api.auth import CallerIdentity, require_caller
    from api.routes.aks import peering as peering_route
    from fastapi.testclient import TestClient

    app, _ = _build_app_with_route(monkeypatch)
    app.dependency_overrides[require_caller] = lambda: CallerIdentity(
        object_id="caller-1",
        tenant_id="tenant-1",
        upn="alice@example.com",
        raw_token="",
        claims={},
    )

    captured: dict[str, Any] = {}

    def _fake_ensure(
        _cred: Any,
        *,
        subscription_id: str,
        cluster_resource_group: str,
        cluster_name: str,
    ) -> dict[str, Any]:
        captured["subscription_id"] = subscription_id
        captured["cluster_resource_group"] = cluster_resource_group
        captured["cluster_name"] = cluster_name
        return {
            "dashboard_vnet": "/.../vnet-elb-dashboard",
            "aks_vnet": "/.../aks-vnet-23268255",
            "peerings": [
                {"direction": "dashboard_to_aks", "name": "peer-a-b", "state": "Connected"},
                {"direction": "aks_to_dashboard", "name": "peer-b-a", "state": "Connected"},
            ],
            "recovery_command": "bash scripts/dev/peer-cluster-network.sh ...",
        }

    monkeypatch.setattr(
        "api.tasks.azure.peering.ensure_vnet_peering_with_cluster", _fake_ensure
    )
    monkeypatch.setattr(peering_route, "LOGGER", peering_route.LOGGER)
    # Stub out the MI credential — the route only passes it through.
    monkeypatch.setattr(
        "api.services.get_credential", lambda: object()
    )

    client = TestClient(app)
    resp = client.post(
        "/api/aks/peer-with-platform",
        json={
            "subscription_id": "sub-1",
            "resource_group": "rg-elb-cluster",
            "cluster_name": "elb-cluster-01",
        },
    )

    assert resp.status_code == 200, resp.text
    assert captured == {
        "subscription_id": "sub-1",
        "cluster_resource_group": "rg-elb-cluster",
        "cluster_name": "elb-cluster-01",
    }
    body = resp.json()
    assert {p["direction"] for p in body["peerings"]} == {
        "dashboard_to_aks",
        "aks_to_dashboard",
    }


def test_route_returns_502_when_helper_raises(monkeypatch) -> None:
    """The helper absorbs per-peering failures itself; an exception bubbling
    out means something else broke (credential, ARM 5xx). Surface that as
    502 with a stable code, not a raw 500.
    """
    from api.auth import CallerIdentity, require_caller
    from fastapi.testclient import TestClient

    app, _ = _build_app_with_route(monkeypatch)
    app.dependency_overrides[require_caller] = lambda: CallerIdentity(
        object_id="caller-1",
        tenant_id="tenant-1",
        upn="alice@example.com",
        raw_token="",
        claims={},
    )

    def _boom(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("ARM down")

    monkeypatch.setattr(
        "api.tasks.azure.peering.ensure_vnet_peering_with_cluster", _boom
    )
    monkeypatch.setattr("api.services.get_credential", lambda: object())

    client = TestClient(app)
    resp = client.post(
        "/api/aks/peer-with-platform",
        json={
            "subscription_id": "sub-1",
            "resource_group": "rg-elb-cluster",
            "cluster_name": "elb-cluster-01",
        },
    )

    assert resp.status_code == 502
    body = resp.json()
    assert body["detail"]["code"] == "vnet_peering_unavailable"
    assert "ARM down" in body["detail"]["message"]


# ---------------------------------------------------------------------------
# provision_aks integration — payload carries the peering summary
# ---------------------------------------------------------------------------


def test_provision_aks_includes_vnet_peering_in_completion(monkeypatch) -> None:
    """The completion payload + final PROGRESS publish must include the
    `vnet_peering` key so the SPA can show a "Network connected" badge
    (or surface `recovery_command` when something failed).
    """
    import api.tasks.azure as azure
    from api.tasks.azure import provision_aks

    class FakeResourceGroups:
        def create_or_update(self, *_args: Any, **_kwargs: Any) -> object:
            return object()

        def get(self, _rg: str) -> object:
            return object()

    class FakeRc:
        resource_groups = FakeResourceGroups()

    class FakePoller:
        def result(self) -> object:
            class _Cluster:
                identity = type("I", (), {"principal_id": "mi-principal"})()
                provisioning_state = "Succeeded"
                node_resource_group = "MC_rg-test_elb-cluster_koreacentral"

            return _Cluster()

    class FakeManagedClusters:
        def begin_create_or_update(
            self, _rg: str, _name: str, _params: object
        ) -> FakePoller:
            return FakePoller()

    class FakeAksClient:
        managed_clusters = FakeManagedClusters()

    publishes: list[dict[str, Any]] = []

    def _capture(state: str, meta: dict[str, Any] | None = None, **_kw: Any) -> None:
        publishes.append({"state": state, "meta": dict(meta or {})})

    monkeypatch.setattr(provision_aks, "update_state", _capture)
    monkeypatch.setattr(azure, "get_credential", lambda: object())
    monkeypatch.setattr(azure, "aks_client", lambda _cred, _sub: FakeAksClient())
    monkeypatch.setattr(azure, "resource_client", lambda _cred, _sub: FakeRc())
    monkeypatch.setattr(
        azure,
        "_ensure_aks_runtime_rbac",
        lambda *_args, **_kwargs: {
            "roles_assigned": [],
            "roles_failed": [],
        },
    )
    monkeypatch.delenv("SHARED_IDENTITY_PRINCIPAL_ID", raising=False)
    monkeypatch.delenv("PLATFORM_PRIVATE_ENDPOINT_SUBNET_ID", raising=False)
    # Stub the peering helper to a deterministic skip so this test stays
    # focused on the wiring contract.
    monkeypatch.setattr(
        azure,
        "_ensure_vnet_peering_with_cluster",
        lambda *_args, **_kwargs: {
            "skipped": True,
            "reason": "dashboard_vnet_id not resolved",
            "recovery_command": "bash scripts/dev/peer-cluster-network.sh ...",
        },
    )

    result = provision_aks.run(
        job_id="job-peer",
        subscription_id="sub-1",
        resource_group="rg-elb-cluster",
        region="koreacentral",
        cluster_name="elb-cluster-01",
        node_sku="Standard_D8s_v3",
        node_count=1,
        system_vm_size="Standard_D2s_v3",
        system_node_count=1,
        acr_resource_group="",
        acr_name="",
        storage_resource_group="",
        storage_account="",
        caller_oid="caller-1",
    )

    assert "vnet_peering" in result
    assert result["vnet_peering"]["skipped"] is True

    completed = [p for p in publishes if p["meta"].get("phase") == "completed"]
    assert completed, publishes
    assert "vnet_peering" in completed[-1]["meta"]
