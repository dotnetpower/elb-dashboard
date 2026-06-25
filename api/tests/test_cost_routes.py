"""Tests for the /api/cost routes (estimate + budget guardrail).

Responsibility: Route contracts — estimate + budget warning, degraded fallback
when the cluster cannot be read, and budget read/write.
Edit boundaries: Test-only; monkeypatches the cluster snapshot, budget store, and
credential so no Azure is touched.
Key entry points: pytest test functions.
Risky contracts: GET /api/cost must never 500 (degrades); budget warning fires when
projected monthly > budget.
Validation: ``uv run pytest -q api/tests/test_cost_routes.py``.
"""

from __future__ import annotations

from typing import Any

import pytest
from api.services.cost import budget_pref as bp
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setenv("AZURE_TENANT_ID", "common")
    monkeypatch.setattr("api.services.get_credential", lambda: None, raising=False)
    from api.main import app

    return TestClient(app)


def _snapshot(**over: Any) -> dict[str, Any]:
    base = {
        "name": "clu",
        "power_state": "Running",
        "node_sku": "Standard_E16s_v5",
        "node_count": 10,
    }
    base.update(over)
    return base


def test_get_cost_over_budget(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "api.services.monitoring.get_aks_cluster_snapshot",
        lambda *a, **k: _snapshot(),
        raising=True,
    )
    monkeypatch.setattr(
        "api.services.auto_stop.get_auto_stop_preference", lambda *a, **k: None, raising=True
    )
    monkeypatch.setattr(
        "api.services.cost.budget_pref.get_budget",
        lambda *a, **k: bp.BudgetPreference("s", "r", "c", monthly_budget_usd=100.0),
        raising=True,
    )
    r = client.get("/api/cost?subscription_id=s&resource_group=r&cluster_name=c")
    assert r.status_code == 200
    body = r.json()
    assert body["estimate"]["priced"] is True
    assert body["budget"]["set"] is True
    # 10 nodes * ~1.008/hr * 730 hr >> 100 USD budget.
    assert body["warning"]["over_budget"] is True


def test_get_cost_degraded_when_not_found(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "api.services.monitoring.get_aks_cluster_snapshot",
        lambda *a, **k: None,
        raising=True,
    )
    r = client.get("/api/cost?subscription_id=s&resource_group=r&cluster_name=c")
    assert r.status_code == 200
    assert r.json()["degraded"] is True


def test_get_cost_no_budget_no_warning(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "api.services.monitoring.get_aks_cluster_snapshot",
        lambda *a, **k: _snapshot(power_state="Stopped"),
        raising=True,
    )
    monkeypatch.setattr(
        "api.services.auto_stop.get_auto_stop_preference", lambda *a, **k: None, raising=True
    )
    monkeypatch.setattr(
        "api.services.cost.budget_pref.get_budget", lambda *a, **k: None, raising=True
    )
    r = client.get("/api/cost?subscription_id=s&resource_group=r&cluster_name=c")
    assert r.status_code == 200
    body = r.json()
    assert body["warning"] is None
    assert body["estimate"]["hourly_usd"] == 0.0  # stopped


def test_put_budget(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_save(pref: Any) -> Any:
        captured["amount"] = pref.monthly_budget_usd
        return pref

    monkeypatch.setattr("api.services.cost.budget_pref.save_budget", fake_save, raising=True)
    r = client.put(
        "/api/cost/budget",
        json={
            "subscription_id": "s",
            "resource_group": "r",
            "cluster_name": "c",
            "monthly_budget_usd": 5000.0,
        },
    )
    assert r.status_code == 200
    assert captured["amount"] == 5000.0


def test_put_budget_rejects_negative(client: TestClient) -> None:
    r = client.put(
        "/api/cost/budget",
        json={
            "subscription_id": "s",
            "resource_group": "r",
            "cluster_name": "c",
            "monthly_budget_usd": -1,
        },
    )
    assert r.status_code == 422  # Field ge=0


def test_get_budget_unset(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "api.services.cost.budget_pref.get_budget", lambda *a, **k: None, raising=True
    )
    r = client.get("/api/cost/budget?subscription_id=s&resource_group=r&cluster_name=c")
    assert r.status_code == 200
    assert r.json() == {"monthly_budget_usd": 0.0, "set": False}
