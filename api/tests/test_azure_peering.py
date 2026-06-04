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
    resource_lists: dict[str, list[str]] | None = None,
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
        "resource_lists": resource_lists or {},
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
            resources = state["resource_lists"].get(rg, state["node_rg_vnets"])
            return [_make_resource(v) for v in resources]

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


def test_helper_peers_target_vnet_and_probes_private_ip(monkeypatch) -> None:
    """A remote VNet can be peered into the AKS auto-VNet and the private
    endpoint path can be probed in the same helper payload.
    """
    aks_vnet = (
        "/subscriptions/sub-1/resourceGroups/MC_rg-elb-cluster_elb-cluster-01_koreacentral/"
        "providers/Microsoft.Network/virtualNetworks/aks-vnet-23268255"
    )
    target_vnet = (
        "/subscriptions/sub-2/resourceGroups/rg-target/providers/"
        "Microsoft.Network/virtualNetworks/vnet-target"
    )

    class _ProbeResponse:
        is_success = True
        status_code = 200
        reason_phrase = "OK"

    def _fake_get(url: str, timeout: float) -> _ProbeResponse:
        assert url == "http://10.224.0.7/openapi.json"
        assert timeout == 2.0
        return _ProbeResponse()

    state = _install_clients(
        monkeypatch,
        node_rg_vnets=[aks_vnet],
        resource_lists={"rg-target": [target_vnet]},
    )
    from api.tasks.azure.peering import ensure_vnet_peering_with_target

    monkeypatch.setattr("api.tasks.azure.peering.httpx.get", _fake_get)

    result = ensure_vnet_peering_with_target(
        object(),
        subscription_id="sub-1",
        cluster_resource_group="rg-elb-cluster",
        cluster_name="elb-cluster-01",
        target_subscription_id="sub-2",
        target_resource_group="rg-target",
        target_vnet_name="vnet-target",
        target_ip="10.224.0.7",
        target_path="/openapi.json",
    )

    assert result["target_vnet"] == target_vnet
    assert result["probe"]["reachable"] is True
    assert result["probe"]["status_code"] == 200
    directions = [p["direction"] for p in result["peerings"]]
    assert directions == ["target_to_aks", "aks_to_target"]
    assert len(state["peering_recorder"]) == 2


def test_target_helper_surfaces_rbac_remediation_on_authz_failure(monkeypatch) -> None:
    """When peering the target VNet is denied by RBAC, the payload must carry a
    precise `rbac_remediation` (Network Contributor scoped to the target VNet,
    with the MI object id parsed from the Azure error) — not just the generic
    platform-to-AKS recovery_command.
    """
    aks_vnet = (
        "/subscriptions/sub-1/resourceGroups/MC_rg-elb-cluster_elb-cluster-01_koreacentral/"
        "providers/Microsoft.Network/virtualNetworks/aks-vnet-23268255"
    )
    target_vnet = (
        "/subscriptions/sub-2/resourceGroups/rg-target/providers/"
        "Microsoft.Network/virtualNetworks/vnet-target"
    )
    authz = Exception(
        "(AuthorizationFailed) The client 'app' with object id "
        "'e51aaab3-eb17-4935-a7eb-446b53a5c445' does not have authorization to "
        "perform action 'Microsoft.Network/virtualNetworks/virtualNetworkPeerings/write'"
    )

    _install_clients(
        monkeypatch,
        node_rg_vnets=[aks_vnet],
        resource_lists={"rg-target": [target_vnet]},
        peering_raises={
            "peer-vnet-target-to-aks-vnet-23268255": authz,
            "peer-aks-vnet-23268255-to-vnet-target": authz,
        },
    )
    monkeypatch.setattr(
        "api.tasks.azure.peering.httpx.get",
        lambda url, timeout: SimpleNamespace(
            is_success=False, status_code=503, reason_phrase="unreachable"
        ),
    )
    from api.tasks.azure.peering import ensure_vnet_peering_with_target

    result = ensure_vnet_peering_with_target(
        object(),
        subscription_id="sub-1",
        cluster_resource_group="rg-elb-cluster",
        cluster_name="elb-cluster-01",
        target_subscription_id="sub-2",
        target_resource_group="rg-target",
        target_vnet_name="vnet-target",
    )

    assert "AuthorizationFailed" in result["error"]
    remediation = result["rbac_remediation"]
    assert remediation["role"] == "Network Contributor"
    assert remediation["scope"] == target_vnet
    # MI object id parsed out of the Azure error and scoped to the target VNet.
    assert "e51aaab3-eb17-4935-a7eb-446b53a5c445" in remediation["command"]
    assert f"--scope {target_vnet}" in remediation["command"]
    assert "az role assignment create" in remediation["command"]


def test_target_helper_omits_rbac_remediation_on_non_authz_error(monkeypatch) -> None:
    """A non-RBAC peering failure records `error` but must NOT fabricate an
    `rbac_remediation` block.
    """
    aks_vnet = (
        "/subscriptions/sub-1/resourceGroups/MC_rg-elb-cluster_elb-cluster-01_koreacentral/"
        "providers/Microsoft.Network/virtualNetworks/aks-vnet-23268255"
    )
    target_vnet = (
        "/subscriptions/sub-2/resourceGroups/rg-target/providers/"
        "Microsoft.Network/virtualNetworks/vnet-target"
    )

    _install_clients(
        monkeypatch,
        node_rg_vnets=[aks_vnet],
        resource_lists={"rg-target": [target_vnet]},
        peering_raises={
            "peer-vnet-target-to-aks-vnet-23268255": Exception(
                "(InternalServerError) try again"
            ),
        },
    )
    monkeypatch.setattr(
        "api.tasks.azure.peering.httpx.get",
        lambda url, timeout: SimpleNamespace(
            is_success=False, status_code=503, reason_phrase="unreachable"
        ),
    )
    from api.tasks.azure.peering import ensure_vnet_peering_with_target

    result = ensure_vnet_peering_with_target(
        object(),
        subscription_id="sub-1",
        cluster_resource_group="rg-elb-cluster",
        cluster_name="elb-cluster-01",
        target_subscription_id="sub-2",
        target_resource_group="rg-target",
        target_vnet_name="vnet-target",
    )

    assert "InternalServerError" in result["error"]
    assert "rbac_remediation" not in result


def _make_byo_cluster(
    *,
    node_rg: str = "MC_rg-elb-cluster_elb-cluster-02_koreacentral",
    subnet_id: str,
):
    """A BYO-subnet AKS cluster: empty MC_ RG, agent pools on an operator subnet."""
    return SimpleNamespace(
        node_resource_group=node_rg,
        agent_pool_profiles=[SimpleNamespace(vnet_subnet_id=subnet_id)],
    )


def test_target_helper_resolves_byo_subnet_vnet_when_node_rg_empty(monkeypatch) -> None:
    """BYO-subnet mode: MC_ RG has no VNet, so the AKS VNet is derived from the
    agent-pool subnet id. Peering with a *different* target VNet still lands.
    """
    aks_vnet = (
        "/subscriptions/sub-1/resourceGroups/rg-elb-dashboard/providers/"
        "Microsoft.Network/virtualNetworks/vnet-elb-dashboard"
    )
    target_vnet = (
        "/subscriptions/sub-1/resourceGroups/rg-vm/providers/"
        "Microsoft.Network/virtualNetworks/ubuntu2204-vnet"
    )

    class _ProbeResponse:
        is_success = False
        status_code = None
        reason_phrase = "timed out"

    state = _install_clients(
        monkeypatch,
        cluster_obj=_make_byo_cluster(subnet_id=f"{aks_vnet}/subnets/snet-aks"),
        node_rg_vnets=[],
        resource_lists={"rg-vm": [target_vnet]},
    )
    monkeypatch.setattr(
        "api.tasks.azure.peering.httpx.get",
        lambda url, timeout: _ProbeResponse(),
    )
    from api.tasks.azure.peering import ensure_vnet_peering_with_target

    result = ensure_vnet_peering_with_target(
        object(),
        subscription_id="sub-1",
        cluster_resource_group="rg-elb-cluster",
        cluster_name="elb-cluster-02",
        target_subscription_id="sub-1",
        target_resource_group="rg-vm",
        target_vnet_name="ubuntu2204-vnet",
    )

    assert "skipped" not in result, result
    assert result["aks_vnet"] == aks_vnet
    assert result["target_vnet"] == target_vnet
    directions = [p["direction"] for p in result["peerings"]]
    assert directions == ["target_to_aks", "aks_to_target"]
    assert len(state["peering_recorder"]) == 2


def test_target_helper_skips_self_peering_when_target_is_aks_vnet(monkeypatch) -> None:
    """BYO-subnet mode: picking the dashboard VNet itself as the target is a
    self-peering ARM error. Skip with a clear reason instead.
    """
    aks_vnet = (
        "/subscriptions/sub-1/resourceGroups/rg-elb-dashboard/providers/"
        "Microsoft.Network/virtualNetworks/vnet-elb-dashboard"
    )

    state = _install_clients(
        monkeypatch,
        cluster_obj=_make_byo_cluster(subnet_id=f"{aks_vnet}/subnets/snet-aks"),
        node_rg_vnets=[],
        resource_lists={"rg-elb-dashboard": [aks_vnet]},
    )
    monkeypatch.setattr(
        "api.tasks.azure.peering.httpx.get",
        lambda url, timeout: SimpleNamespace(
            is_success=False, status_code=None, reason_phrase="timed out"
        ),
    )
    from api.tasks.azure.peering import ensure_vnet_peering_with_target

    result = ensure_vnet_peering_with_target(
        object(),
        subscription_id="sub-1",
        cluster_resource_group="rg-elb-cluster",
        cluster_name="elb-cluster-02",
        target_subscription_id="sub-1",
        target_resource_group="rg-elb-dashboard",
        target_vnet_name="vnet-elb-dashboard",
    )

    assert result["skipped"] is True
    assert result["reason"] == "target_vnet_is_aks_vnet"
    assert result["message"]
    assert state["peering_recorder"] == []


def test_cluster_helper_skips_when_aks_shares_dashboard_vnet(monkeypatch) -> None:
    """BYO-subnet provision: the AKS VNet IS the dashboard platform VNet, so the
    provision-time peering is a no-op self-peer. Skip cleanly.
    """
    dash_vnet = (
        "/subscriptions/sub-1/resourceGroups/rg-elb-dashboard/providers/"
        "Microsoft.Network/virtualNetworks/vnet-elb-dashboard"
    )
    state = _install_clients(
        monkeypatch,
        cluster_obj=_make_byo_cluster(subnet_id=f"{dash_vnet}/subnets/snet-aks"),
        node_rg_vnets=[],
    )
    from api.tasks.azure.peering import ensure_vnet_peering_with_cluster

    result = ensure_vnet_peering_with_cluster(
        object(),
        subscription_id="sub-1",
        cluster_resource_group="rg-elb-cluster",
        cluster_name="elb-cluster-02",
        dashboard_vnet_id=dash_vnet,
    )

    assert result["skipped"] is True
    assert result["reason"] == "aks_shares_dashboard_vnet"
    assert result["aks_vnet"] == dash_vnet
    assert state["peering_recorder"] == []


# ---------------------------------------------------------------------------
# probe_private_ip — SSRF chokepoint
# ---------------------------------------------------------------------------


def test_probe_private_ip_refuses_non_private_targets(monkeypatch) -> None:
    """The probe must refuse any address outside RFC1918 even when called
    directly by trusted code paths (provision_aks, ensure_vnet_peering_*).
    Verifies the chokepoint without crossing the network."""

    from api.tasks.azure.peering import probe_private_ip

    called = {"n": 0}

    def _spy(*_a: Any, **_kw: Any) -> Any:
        called["n"] += 1
        raise AssertionError("httpx.get must not be called for hostile targets")

    monkeypatch.setattr("api.tasks.azure.peering.httpx.get", _spy)

    for hostile in (
        "169.254.169.254",  # IMDS
        "127.0.0.1",  # loopback
        "1.1.1.1",  # public
        "224.0.0.1",  # multicast
        "::1",  # IPv6 loopback (IPv6 rejected outright)
        "fd00::1",  # IPv6 ULA — private but URL builder cannot express it
        "::ffff:169.254.169.254",  # IPv4-mapped IMDS bypass attempt
        "not-an-ip",
    ):
        out = probe_private_ip(target_ip=hostile, target_path="/openapi.json")
        assert out["reachable"] is False
        assert out["url"] == ""
        assert out["status_code"] is None
    assert called["n"] == 0


def test_probe_private_ip_refuses_control_characters_in_path(monkeypatch) -> None:
    from api.tasks.azure.peering import probe_private_ip

    def _spy(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError("httpx.get must not be called for unsafe paths")

    monkeypatch.setattr("api.tasks.azure.peering.httpx.get", _spy)

    out = probe_private_ip(target_ip="10.224.0.7", target_path="/x\r\nHost: evil")
    assert out["reachable"] is False
    assert "control characters" in out["message"]

    out = probe_private_ip(target_ip="10.224.0.7", target_path="/" + "a" * 300)
    assert out["reachable"] is False
    assert "too long" in out["message"]


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
