"""Tests for the Settings → Performance HTTP routes.

Responsibility: Verify GET defaults to ``ephemeral`` without a row, PUT persists a
    valid mode, and an invalid mode is rejected by the Pydantic enum.
Edit boundaries: Route shaping only; persistence is covered by
    ``test_performance_pref.py``.
Key entry points: ``test_get_defaults_ephemeral``, ``test_put_then_get_round_trip``,
    ``test_put_rejects_invalid_mode``.
Risky contracts: Routes must enforce ``require_caller``; GET must never 404.
Validation: ``uv run pytest -q api/tests/test_settings_performance.py``.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

_SUB = "00000000-0000-0000-0000-000000000001"


@pytest.fixture()
def client(tmp_path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setenv("AZURE_TENANT_ID", "common")
    monkeypatch.setenv("API_CLIENT_ID", "00000000-0000-0000-0000-000000000000")
    monkeypatch.delenv("CONTAINER_APP_NAME", raising=False)
    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    monkeypatch.setenv("ELB_LOCAL_STATE_DIR", str(tmp_path))
    from api.main import app

    return TestClient(app)


def test_get_defaults_ephemeral(client: TestClient) -> None:
    response = client.get(
        "/api/settings/performance",
        params={
            "subscription_id": _SUB,
            "resource_group": "rg-elb",
            "cluster_name": "aks-elb",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["preference"] is None
    assert body["warm_cache_mode"] == "ephemeral"


def test_put_then_get_round_trip(client: TestClient) -> None:
    put = client.put(
        "/api/settings/performance",
        json={
            "subscription_id": _SUB,
            "resource_group": "rg-elb",
            "cluster_name": "aks-elb",
            "warm_cache_mode": "node_disk",
        },
    )
    assert put.status_code == 200
    assert put.json()["preference"]["warm_cache_mode"] == "node_disk"

    get = client.get(
        "/api/settings/performance",
        params={
            "subscription_id": _SUB,
            "resource_group": "rg-elb",
            "cluster_name": "aks-elb",
        },
    )
    assert get.status_code == 200
    assert get.json()["warm_cache_mode"] == "node_disk"


def test_put_rejects_invalid_mode(client: TestClient) -> None:
    response = client.put(
        "/api/settings/performance",
        json={
            "subscription_id": _SUB,
            "resource_group": "rg-elb",
            "cluster_name": "aks-elb",
            "warm_cache_mode": "turbo",
        },
    )
    assert response.status_code == 422


def test_get_rejects_bad_subscription(client: TestClient) -> None:
    response = client.get(
        "/api/settings/performance",
        params={
            "subscription_id": "not-a-guid",
            "resource_group": "rg-elb",
            "cluster_name": "aks-elb",
        },
    )
    assert response.status_code == 400
