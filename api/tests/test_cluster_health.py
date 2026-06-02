"""Tests for the per-cluster ARM health gate.

Responsibility: Tests for the per-cluster ARM health gate.
Edit boundaries: Fakes only; no live ARM credentials.
Key entry points: `test_*`.
Risky contracts: `degraded_reason` codes (`cluster_stopped`, `cluster_not_found`)
are part of the SPA banner contract — renaming requires a coordinated SPA
change.
Validation: `uv run pytest -q api/tests/test_cluster_health.py`.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from api.services import cluster_health, monitor_cache


@pytest.fixture(autouse=True)
def _reset_caches() -> Any:
    monitor_cache.reset_monitor_snapshot_cache()
    yield
    monitor_cache.reset_monitor_snapshot_cache()


class _FakeClient:
    def __init__(self, get_result: Any) -> None:
        self.calls = 0
        self._get_result = get_result
        self.managed_clusters = self

    def get(self, resource_group: str, cluster_name: str) -> Any:
        self.calls += 1
        if isinstance(self._get_result, Exception):
            raise self._get_result
        return self._get_result


def _install_fake_aks_client(monkeypatch: pytest.MonkeyPatch, fake: _FakeClient) -> None:
    monkeypatch.setattr(cluster_health, "aks_client", lambda *_a, **_k: fake)


def test_get_cluster_health_running_returns_healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(
        SimpleNamespace(
            power_state=SimpleNamespace(code="Running"),
            provisioning_state="Succeeded",
        )
    )
    _install_fake_aks_client(monkeypatch, fake)

    health = cluster_health.get_cluster_health(
        credential=object(),
        subscription_id="sub",
        resource_group="rg",
        cluster_name="elb",
    )

    assert health == cluster_health.ClusterHealth(
        healthy=True,
        exists=True,
        power_state="Running",
        provisioning_state="Succeeded",
        reason=None,
    )
    assert fake.calls == 1


def test_get_cluster_health_stopped_returns_skip_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeClient(
        SimpleNamespace(
            power_state=SimpleNamespace(code="Stopped"),
            provisioning_state="Succeeded",
        )
    )
    _install_fake_aks_client(monkeypatch, fake)

    health = cluster_health.get_cluster_health(
        credential=object(),
        subscription_id="sub",
        resource_group="rg",
        cluster_name="elb",
    )

    assert health["healthy"] is False
    assert health["reason"] == "cluster_stopped"
    assert health["power_state"] == "Stopped"


def test_get_cluster_health_missing_returns_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from azure.core.exceptions import ResourceNotFoundError

    fake = _FakeClient(ResourceNotFoundError("not found"))
    _install_fake_aks_client(monkeypatch, fake)

    health = cluster_health.get_cluster_health(
        credential=object(),
        subscription_id="sub",
        resource_group="rg",
        cluster_name="elb",
    )

    assert health["healthy"] is False
    assert health["exists"] is False
    assert health["reason"] == "cluster_not_found"
    assert health["power_state"] is None


def test_get_cluster_health_arm_unreachable_degrades_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ARM itself is unreachable we MUST NOT inject a synthetic skip —
    callers degrade open and let the K8s call's own error handling run.
    """
    from azure.core.exceptions import ServiceRequestError

    fake = _FakeClient(ServiceRequestError("network down"))
    _install_fake_aks_client(monkeypatch, fake)

    health = cluster_health.get_cluster_health(
        credential=object(),
        subscription_id="sub",
        resource_group="rg",
        cluster_name="elb",
    )

    assert health["healthy"] is True
    assert health["reason"] is None


def test_get_cluster_health_caches_arm_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Repeated polls in the TTL window must hit the cache, not ARM."""
    fake = _FakeClient(
        SimpleNamespace(
            power_state=SimpleNamespace(code="Running"),
            provisioning_state="Succeeded",
        )
    )
    _install_fake_aks_client(monkeypatch, fake)

    for _ in range(5):
        cluster_health.get_cluster_health(
            credential=object(),
            subscription_id="sub",
            resource_group="rg",
            cluster_name="elb",
            ttl_seconds=30.0,
        )

    assert fake.calls == 1


def test_cached_snapshot_with_cluster_gate_skips_stopped_cluster(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate short-circuits — the loader (which would hit the K8s API) is
    never invoked when ARM says the cluster is Stopped.
    """
    fake = _FakeClient(
        SimpleNamespace(
            power_state=SimpleNamespace(code="Stopped"),
            provisioning_state="Succeeded",
        )
    )
    _install_fake_aks_client(monkeypatch, fake)

    loader_calls = 0

    def loader() -> dict[str, Any]:
        nonlocal loader_calls
        loader_calls += 1
        raise AssertionError("loader must not run when cluster is Stopped")

    result = cluster_health.cached_snapshot_with_cluster_gate(
        "monitor:aks:top-nodes:sub:rg:elb",
        loader,
        credential=object(),
        subscription_id="sub",
        resource_group="rg",
        cluster_name="elb",
        empty={"nodes": []},
    )

    assert loader_calls == 0
    assert result["degraded"] is True
    assert result["degraded_reason"] == "cluster_stopped"
    assert result["power_state"] == "Stopped"
    assert result["nodes"] == []


def test_cached_snapshot_with_cluster_gate_skips_missing_cluster(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from azure.core.exceptions import ResourceNotFoundError

    fake = _FakeClient(ResourceNotFoundError("not found"))
    _install_fake_aks_client(monkeypatch, fake)

    def loader() -> dict[str, Any]:
        raise AssertionError("loader must not run when cluster is missing")

    result = cluster_health.cached_snapshot_with_cluster_gate(
        "monitor:aks:warmup-status:sub:rg:gone",
        loader,
        credential=object(),
        subscription_id="sub",
        resource_group="rg",
        cluster_name="gone",
        empty={"databases": []},
    )

    assert result["degraded"] is True
    assert result["degraded_reason"] == "cluster_not_found"
    assert result["databases"] == []
    assert "power_state" not in result  # None → omitted


def test_cached_snapshot_with_cluster_gate_runs_loader_when_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeClient(
        SimpleNamespace(
            power_state=SimpleNamespace(code="Running"),
            provisioning_state="Succeeded",
        )
    )
    _install_fake_aks_client(monkeypatch, fake)

    loader_calls = 0

    def loader() -> dict[str, Any]:
        nonlocal loader_calls
        loader_calls += 1
        return {"nodes": [{"name": "agent-0"}]}

    result = cluster_health.cached_snapshot_with_cluster_gate(
        "monitor:aks:top-nodes:sub:rg:healthy",
        loader,
        credential=object(),
        subscription_id="sub",
        resource_group="rg",
        cluster_name="healthy",
        empty={"nodes": []},
    )

    assert loader_calls == 1
    assert result["nodes"] == [{"name": "agent-0"}]
    assert result.get("degraded") is not True


def test_cached_snapshot_with_cluster_gate_multi_cluster_isolation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stopping one cluster must not affect monitoring of a healthy sibling."""
    cluster_states = {
        "stopped-one": SimpleNamespace(
            power_state=SimpleNamespace(code="Stopped"),
            provisioning_state="Succeeded",
        ),
        "running-two": SimpleNamespace(
            power_state=SimpleNamespace(code="Running"),
            provisioning_state="Succeeded",
        ),
    }

    class _DispatchClient:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.managed_clusters = self

        def get(self, resource_group: str, cluster_name: str) -> Any:
            self.calls.append(cluster_name)
            return cluster_states[cluster_name]

    fake = _DispatchClient()
    monkeypatch.setattr(cluster_health, "aks_client", lambda *_a, **_k: fake)

    loader_calls: dict[str, int] = {"stopped-one": 0, "running-two": 0}

    def make_loader(name: str):
        def loader() -> dict[str, Any]:
            loader_calls[name] += 1
            return {"nodes": [{"name": f"{name}-node"}]}

        return loader

    stopped = cluster_health.cached_snapshot_with_cluster_gate(
        "monitor:aks:top-nodes:sub:rg:stopped-one",
        make_loader("stopped-one"),
        credential=object(),
        subscription_id="sub",
        resource_group="rg",
        cluster_name="stopped-one",
        empty={"nodes": []},
    )
    running = cluster_health.cached_snapshot_with_cluster_gate(
        "monitor:aks:top-nodes:sub:rg:running-two",
        make_loader("running-two"),
        credential=object(),
        subscription_id="sub",
        resource_group="rg",
        cluster_name="running-two",
        empty={"nodes": []},
    )

    assert loader_calls == {"stopped-one": 0, "running-two": 1}
    assert stopped["degraded_reason"] == "cluster_stopped"
    assert running["nodes"] == [{"name": "running-two-node"}]
    assert "degraded" not in running or running["degraded"] is not True
