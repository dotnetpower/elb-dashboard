from __future__ import annotations

from types import SimpleNamespace

from api.services import monitoring


def _pool(name: str, mode: str, vm_size: str, count: int) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        mode=mode,
        vm_size=vm_size,
        count=count,
        min_count=None,
        max_count=None,
        os_type="Linux",
        power_state=SimpleNamespace(code="Running"),
        enable_auto_scaling=False,
    )


def _cluster(pools: list[SimpleNamespace]) -> SimpleNamespace:
    return SimpleNamespace(
        name="aks-elb",
        location="koreacentral",
        kubernetes_version="1.30.0",
        provisioning_state="Succeeded",
        power_state=SimpleNamespace(code="Running"),
        agent_pool_profiles=pools,
        identity_profile=None,
        network_profile=None,
        fqdn="aks.example.local",
    )


def test_list_aks_clusters_summarises_blastpool_not_systempool(monkeypatch) -> None:
    pools = [
        _pool("systempool", "System", "Standard_D4s_v5", 1),
        _pool("blastpool", "User", "Standard_E32s_v5", 3),
    ]
    fake_client = SimpleNamespace(
        managed_clusters=SimpleNamespace(
            list_by_resource_group=lambda _resource_group: [_cluster(pools)]
        )
    )
    monkeypatch.setattr(monitoring, "aks_client", lambda _credential, _subscription_id: fake_client)

    clusters = monitoring.list_aks_clusters(object(), "sub", "rg-elb")

    assert clusters[0]["node_sku"] == "Standard_E32s_v5"
    assert clusters[0]["node_count"] == 3
    assert clusters[0]["agent_pools"][0]["name"] == "systempool"


def test_list_aks_clusters_falls_back_to_user_pool_without_blastpool(monkeypatch) -> None:
    pools = [
        _pool("systempool", "System", "Standard_D4s_v5", 1),
        _pool("gpuuser", "User", "Standard_NC24ads_A100_v4", 2),
    ]
    fake_client = SimpleNamespace(
        managed_clusters=SimpleNamespace(
            list_by_resource_group=lambda _resource_group: [_cluster(pools)]
        )
    )
    monkeypatch.setattr(monitoring, "aks_client", lambda _credential, _subscription_id: fake_client)

    clusters = monitoring.list_aks_clusters(object(), "sub", "rg-elb")

    assert clusters[0]["node_sku"] == "Standard_NC24ads_A100_v4"
    assert clusters[0]["node_count"] == 2
