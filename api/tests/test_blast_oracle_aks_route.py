"""Tests for the AKS power-state gate on `/api/blast/databases/{db}/oracle`.

Responsibility: Pin the explicit 409 `aks_unavailable` response the order
    oracle build returns when the target AKS cluster is not Running, and that
    the gate degrades open (does not 409) when the ARM health probe raises.
Edit boundaries: Stubs the credential, the local-storage-access helper, and
    `get_cluster_health`; the unhealthy case returns before any Storage or
    K8s call so nothing else needs mocking.
Key entry points: `test_oracle_returns_409_when_cluster_stopped`,
    `test_oracle_degrades_open_when_health_probe_raises`.
Risky contracts: The 409 detail object's `code: aks_unavailable` is the SPA
    hook for the actionable "start the cluster" hint — renaming it breaks the
    Build Oracle error toast.
Validation: `uv run pytest -q api/tests/test_blast_oracle_aks_route.py`.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setenv("AZURE_TENANT_ID", "common")
    monkeypatch.setenv("API_CLIENT_ID", "00000000-0000-0000-0000-000000000000")
    from api.main import app

    return TestClient(app)


_BODY = {
    "subscription_id": "00000000-0000-0000-0000-000000000000",
    "resource_group": "rg-elb",
    "account_name": "stelbtest",
    "cluster_name": "elb-cluster",
    "acr_name": "acrelbtest",
}


def _patch_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "api.services.get_credential",
        lambda *_a, **_kw: object(),
        raising=True,
    )
    monkeypatch.setattr(
        "api.routes.blast.databases._maybe_open_local_storage_access",
        lambda *_a, **_kw: None,
        raising=True,
    )


def test_oracle_returns_409_when_cluster_stopped(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_credential(monkeypatch)
    monkeypatch.setattr(
        "api.services.cluster_health.get_cluster_health",
        lambda *_a, **_kw: {
            "healthy": False,
            "exists": True,
            "power_state": "Stopped",
            "reason": "cluster_stopped",
        },
        raising=True,
    )

    resp = client.post("/api/blast/databases/core_nt/oracle", json=_BODY)

    assert resp.status_code == 409, resp.text
    detail = resp.json()
    assert detail["code"] == "aks_unavailable"
    assert detail["cluster_power_state"] == "Stopped"
    assert detail["cluster_reason"] == "cluster_stopped"


def test_oracle_degrades_open_when_health_probe_raises(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_credential(monkeypatch)

    def _boom(*_a: Any, **_kw: Any) -> dict[str, Any]:
        raise RuntimeError("ARM unreachable")

    monkeypatch.setattr(
        "api.services.cluster_health.get_cluster_health", _boom, raising=True
    )
    # Storage listing returns no match → the route 404s the DB rather than
    # 409-ing on the cluster. The point is that the health probe raising does
    # NOT short-circuit into a 409.
    monkeypatch.setattr(
        "api.services.storage.data.list_databases",
        lambda *_a, **_kw: [],
        raising=True,
    )

    resp = client.post("/api/blast/databases/core_nt/oracle", json=_BODY)

    assert resp.status_code != 409
    if resp.status_code >= 400:
        body = resp.json()
        if isinstance(body, dict):
            assert body.get("code") != "aks_unavailable"
