"""Tests for the AKS node-subnet NSG ingress reconcile helper.

Responsibility: Verify `ensure_ingress_lb_inbound_rule` opens the BYO
    node-subnet NSG to the ingress LB VIP, skips gracefully for managed-VNet
    and NSG-less subnets, and that `first_node_subnet_id` picks the first
    non-empty agent-pool subnet.
Edit boundaries: Test-only. Mirrors the contract in
    `api.services.aks.node_subnet_nsg`.
Key entry points: pytest test functions below.
Risky contracts: The ensured rule must stay destination-scoped to the LB VIP
    on ports 80/443 only — assert the exact rule payload.
Validation: `uv run pytest -q api/tests/test_node_subnet_nsg.py`.
"""

from __future__ import annotations

from typing import Any

import pytest
from api.services.aks import node_subnet_nsg as mod


class _Profile:
    def __init__(self, subnet_id: str | None) -> None:
        self.vnet_subnet_id = subnet_id


class _Cluster:
    def __init__(self, subnet_ids: list[str | None]) -> None:
        self.agent_pool_profiles = [_Profile(s) for s in subnet_ids]


def test_first_node_subnet_id_picks_first_non_empty() -> None:
    cluster = _Cluster([None, "", "  ", "/subscriptions/s/x"])
    assert mod.first_node_subnet_id(cluster) == "/subscriptions/s/x"


def test_first_node_subnet_id_managed_vnet_returns_empty() -> None:
    assert mod.first_node_subnet_id(_Cluster([None, ""])) == ""
    # cluster object without agent_pool_profiles attribute
    assert mod.first_node_subnet_id(object()) == ""


def test_ensure_skips_for_managed_vnet() -> None:
    result = mod.ensure_ingress_lb_inbound_rule(
        credential=object(),
        subscription_id="sub-1",
        node_subnet_id="",
        lb_ip="20.0.0.1",
    )
    assert result == {"status": "skipped", "reason": "managed_vnet"}


def test_ensure_requires_lb_ip() -> None:
    with pytest.raises(ValueError, match="lb_ip is required"):
        mod.ensure_ingress_lb_inbound_rule(
            credential=object(),
            subscription_id="sub-1",
            node_subnet_id="/subscriptions/s/resourceGroups/rg/providers/"
            "Microsoft.Network/virtualNetworks/v/subnets/snet-aks",
            lb_ip="",
        )


def test_ensure_rejects_malformed_subnet_id() -> None:
    with pytest.raises(ValueError, match="cannot parse node subnet id"):
        mod.ensure_ingress_lb_inbound_rule(
            credential=object(),
            subscription_id="sub-1",
            node_subnet_id="not-a-subnet-id",
            lb_ip="20.0.0.1",
        )


class _Poller:
    def result(self) -> None:
        return None


class _SecurityRules:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, dict[str, Any]]] = []

    def begin_create_or_update(
        self, rg: str, nsg: str, rule: str, params: dict[str, Any]
    ) -> _Poller:
        self.calls.append((rg, nsg, rule, params))
        return _Poller()


class _Subnets:
    def __init__(self, nsg_id: str | None) -> None:
        self._nsg_id = nsg_id

    def get(self, rg: str, vnet: str, subnet: str) -> Any:
        nsg = type("NSG", (), {"id": self._nsg_id})() if self._nsg_id is not None else None
        return type("Subnet", (), {"network_security_group": nsg})()


class _NetworkClient:
    def __init__(self, nsg_id: str | None) -> None:
        self.subnets = _Subnets(nsg_id)
        self.security_rules = _SecurityRules()


_SUBNET_ID = (
    "/subscriptions/sub-net/resourceGroups/rg-net/providers/"
    "Microsoft.Network/virtualNetworks/vnet-elb/subnets/snet-aks"
)


def test_ensure_skips_when_subnet_has_no_nsg(monkeypatch) -> None:
    client = _NetworkClient(nsg_id=None)
    monkeypatch.setattr(mod, "network_client", lambda *_a, **_kw: client)
    result = mod.ensure_ingress_lb_inbound_rule(
        credential=object(),
        subscription_id="sub-1",
        node_subnet_id=_SUBNET_ID,
        lb_ip="20.0.0.1",
    )
    assert result == {"status": "skipped", "reason": "no_subnet_nsg"}
    assert client.security_rules.calls == []


def test_ensure_creates_rule_on_byo_subnet_nsg(monkeypatch) -> None:
    nsg_id = (
        "/subscriptions/sub-net/resourceGroups/rg-net/providers/"
        "Microsoft.Network/networkSecurityGroups/vnet-elb-snet-aks-nsg"
    )
    client = _NetworkClient(nsg_id=nsg_id)
    captured_subs: list[str] = []

    def _fake_network_client(_cred: Any, sub: str) -> _NetworkClient:
        captured_subs.append(sub)
        return client

    monkeypatch.setattr(mod, "network_client", _fake_network_client)

    result = mod.ensure_ingress_lb_inbound_rule(
        credential=object(),
        subscription_id="cluster-sub",
        node_subnet_id=_SUBNET_ID,
        lb_ip="20.249.196.28",
    )

    assert result == {
        "status": "ensured",
        "nsg": "vnet-elb-snet-aks-nsg",
        "rule": mod.INGRESS_LB_INBOUND_RULE_NAME,
        "lb_ip": "20.249.196.28",
    }
    # The network client must target the subnet's own subscription, not the
    # cluster subscription.
    assert captured_subs == ["sub-net"]

    assert len(client.security_rules.calls) == 1
    rg, nsg, rule, params = client.security_rules.calls[0]
    assert rg == "rg-net"
    assert nsg == "vnet-elb-snet-aks-nsg"
    assert rule == mod.INGRESS_LB_INBOUND_RULE_NAME
    assert params["priority"] == mod.INGRESS_LB_INBOUND_RULE_PRIORITY
    assert params["direction"] == "Inbound"
    assert params["access"] == "Allow"
    assert params["protocol"] == "Tcp"
    assert params["source_address_prefix"] == "Internet"
    assert params["destination_address_prefix"] == "20.249.196.28"
    assert params["destination_port_ranges"] == ["80", "443"]
