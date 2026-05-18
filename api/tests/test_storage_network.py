from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from api.services import storage_network


class _Poller:
    def __init__(self, result: object) -> None:
        self._result = result

    def result(self) -> object:
        return self._result


class _PrivateEndpoints:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def begin_create_or_update(
        self,
        resource_group: str,
        name: str,
        payload: dict[str, Any],
    ) -> _Poller:
        self.calls.append((resource_group, name, payload))
        return _Poller(SimpleNamespace(id=f"/pe/{name}"))


class _PrivateDnsZoneGroups:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, dict[str, Any]]] = []

    def begin_create_or_update(
        self,
        resource_group: str,
        endpoint_name: str,
        name: str,
        payload: dict[str, Any],
    ) -> _Poller:
        self.calls.append((resource_group, endpoint_name, name, payload))
        return _Poller(SimpleNamespace(id=f"/zoneGroups/{endpoint_name}/{name}"))


class _NetworkClient:
    def __init__(self) -> None:
        self.private_endpoints = _PrivateEndpoints()
        self.private_dns_zone_groups = _PrivateDnsZoneGroups()


class _StorageAccounts:
    def get_properties(self, resource_group: str, account_name: str) -> SimpleNamespace:
        return SimpleNamespace(
            id=(
                "/subscriptions/sub/resourceGroups/rg-workload/providers/"
                f"Microsoft.Storage/storageAccounts/{account_name}"
            )
        )


class _StorageClient:
    def __init__(self) -> None:
        self.storage_accounts = _StorageAccounts()


def test_ensure_workload_storage_private_endpoints_creates_blob_and_dfs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    network = _NetworkClient()
    monkeypatch.setattr(storage_network, "network_client", lambda *_args: network)
    monkeypatch.setattr(storage_network, "storage_client", lambda *_args: _StorageClient())

    ensured = storage_network.ensure_workload_storage_private_endpoints(
        credential=object(),
        subscription_id="sub",
        storage_resource_group="rg-workload",
        account_name="elbstg01",
        location="koreacentral",
        private_endpoint_subnet_id=(
            "/subscriptions/sub/resourceGroups/rg-platform/providers/Microsoft.Network/"
            "virtualNetworks/vnet-elb/subnets/snet-private-endpoints"
        ),
        private_dns_zone_resource_group="rg-platform",
    )

    assert [item[1] for item in network.private_endpoints.calls] == [
        "pe-elbstg01-blob",
        "pe-elbstg01-dfs",
    ]
    assert [item["group"] for item in ensured] == ["blob", "dfs"]
    assert all(call[0] == "rg-platform" for call in network.private_endpoints.calls)
    assert all(call[0] == "rg-platform" for call in network.private_dns_zone_groups.calls)
    blob_payload = network.private_endpoints.calls[0][2]
    assert blob_payload["private_link_service_connections"][0]["group_ids"] == ["blob"]
    dfs_zone_payload = network.private_dns_zone_groups.calls[1][3]
    assert dfs_zone_payload["private_dns_zone_configs"][0]["private_dns_zone_id"].endswith(
        "/resourceGroups/rg-platform/providers/Microsoft.Network/privateDnsZones/"
        "privatelink.dfs.core.windows.net"
    )


def test_ensure_workload_storage_private_endpoints_skips_when_unconfigured() -> None:
    ensured = storage_network.ensure_workload_storage_private_endpoints(
        credential=object(),
        subscription_id="sub",
        storage_resource_group="rg-workload",
        account_name="elbstg01",
        location="koreacentral",
        private_endpoint_subnet_id="",
        private_dns_zone_resource_group="rg-platform",
    )

    assert ensured == []
