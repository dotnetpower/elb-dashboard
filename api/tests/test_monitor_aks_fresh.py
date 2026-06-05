"""Tests for the `/api/monitor/aks?fresh=true` cache-bypass contract.

Responsibility: Verify that the AKS cluster-list route serves the monitor cache
    by default but re-queries ARM synchronously when `fresh=true`, so the SPA can
    surface a settled `provisioning_state` while a start/stop transition is in
    flight without waiting out the 30 s cache TTL.
Edit boundaries: Keep assertions on route caching behaviour; cluster serialisation
    is covered by `test_monitoring_aks_subwide.py`.
Key entry points: `test_default_serves_cache`, `test_fresh_param_bypasses_cache`.
Risky contracts: `fresh` must map to `cached_snapshot(force=...)`; the cluster
    list `provisioning_state` is what drives the SPA "Starting" label.
Validation: `uv run pytest -q api/tests/test_monitor_aks_fresh.py`.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setenv("SIDECAR_REPORTER_DISABLED", "true")

    from api import main as api_main
    from api.routes import monitor as monitor_package
    from api.services import monitor_cache

    monitor_cache.reset_monitor_snapshot_cache()
    monkeypatch.setattr(monitor_package, "get_credential", lambda: object())
    yield TestClient(api_main.create_app())
    monitor_cache.reset_monitor_snapshot_cache()


def _url(*, fresh: bool = False) -> str:
    base = "/api/monitor/aks?subscription_id=sub-1&resource_group=rg-elb-01"
    return base + "&fresh=true" if fresh else base


def test_default_serves_cache(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def fake_list(_cred: object, _sub: str, _rg: str) -> list[dict[str, object]]:
        calls["n"] += 1
        state = "Starting" if calls["n"] == 1 else "Succeeded"
        return [{"name": "elb-01", "power_state": "Running", "provisioning_state": state}]

    from api.services import monitoring as monitoring_svc

    monkeypatch.setattr(monitoring_svc, "list_aks_clusters", fake_list)

    first = client.get(_url())
    assert first.status_code == 200
    assert first.json()["clusters"][0]["provisioning_state"] == "Starting"

    # A second normal poll within the TTL serves the cached "Starting" reading
    # (loader not called again), so the SPA would keep showing Starting.
    second = client.get(_url())
    assert second.status_code == 200
    assert second.json()["clusters"][0]["provisioning_state"] == "Starting"
    assert calls["n"] == 1


def test_fresh_param_bypasses_cache(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"n": 0}

    def fake_list(_cred: object, _sub: str, _rg: str) -> list[dict[str, object]]:
        calls["n"] += 1
        state = "Starting" if calls["n"] == 1 else "Succeeded"
        return [{"name": "elb-01", "power_state": "Running", "provisioning_state": state}]

    from api.services import monitoring as monitoring_svc

    monkeypatch.setattr(monitoring_svc, "list_aks_clusters", fake_list)

    # Prime the cache with the transitional reading.
    primed = client.get(_url())
    assert primed.json()["clusters"][0]["provisioning_state"] == "Starting"
    assert calls["n"] == 1

    # fresh=true bypasses the cache and re-queries ARM, surfacing the settled state.
    fresh = client.get(_url(fresh=True))
    assert fresh.status_code == 200
    assert fresh.json()["clusters"][0]["provisioning_state"] == "Succeeded"
    assert calls["n"] == 2

    # The forced refresh updated the cache, so the next normal poll is also settled.
    after = client.get(_url())
    assert after.json()["clusters"][0]["provisioning_state"] == "Succeeded"
    assert calls["n"] == 2
