"""Route contract tests for /api/diagnostics/{category}.

Responsibility: Verify auth gating, category validation, the report schema, the
    sanitisation pass, and the failure → indeterminate path without live Azure.
Edit boundaries: Route response shaping + engine wiring; rule specifics live in
    `test_diagnostics_rules.py`.
Key entry points: the `test_*` functions below.
Risky contracts: Unknown category → 404; missing subscription → 400; a fetch
    failure surfaces as an `indeterminate` finding, never a 500 or an empty `ok`.
Validation: `uv run pytest -q api/tests/test_diagnostics_route.py`.
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
    return TestClient(api_main.create_app())


def _url(category: str = "reliability") -> str:
    return (
        f"/api/diagnostics/{category}"
        "?subscription_id=sub-1"
        "&workload_resource_group=rg-elb"
        "&storage_account_name=stelb"
        "&acr_resource_group=rg-elb"
        "&acr_name=acrelb"
    )


def test_unknown_category_is_404(client: TestClient) -> None:
    resp = client.get("/api/diagnostics/bogus?subscription_id=sub-1")
    assert resp.status_code == 404


def test_operational_category_is_supported(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from api.services import monitoring as monitoring_svc

    monkeypatch.setattr(
        monitoring_svc, "list_aks_clusters_detail_in_subscription", lambda *a, **k: []
    )
    resp = client.get("/api/diagnostics/operational?subscription_id=sub-1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["category"] == "operational"
    ids = {f["id"] for f in body["findings"]}
    # No clusters → aks.runtime info finding present.
    assert "aks.runtime" in ids


def test_missing_subscription_is_400(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AZURE_SUBSCRIPTION_ID", raising=False)
    resp = client.get("/api/diagnostics/reliability")
    assert resp.status_code == 400


def test_reliability_report_shape(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from api.services import monitoring as monitoring_svc

    monkeypatch.setattr(
        monitoring_svc,
        "list_aks_clusters_detail_in_subscription",
        lambda *a, **k: [
            {
                "name": "elb-cluster-01",
                "resource_group": "rg-elb",
                "provisioning_state": "Succeeded",
                "power_state": "Running",
                "k8s_version": "1.30.4",
                "agent_pools": [{"mode": "User", "enable_auto_scaling": True}],
            }
        ],
    )
    monkeypatch.setattr(
        monitoring_svc,
        "get_storage_account_detail",
        lambda *a, **k: {"name": "stelb", "sku": "Standard_LRS"},
    )
    monkeypatch.setattr(
        monitoring_svc,
        "get_acr_registry_detail",
        lambda *a, **k: {"name": "acrelb", "sku": "Basic"},
    )

    resp = client.get(_url())
    assert resp.status_code == 200
    body = resp.json()
    assert body["category"] == "reliability"
    assert "findings" in body and isinstance(body["findings"], list)
    assert "rollup" in body
    ids = {f["id"] for f in body["findings"]}
    assert {"aks.provisioning_state", "storage.redundancy", "acr.sku"} <= ids
    # Findings are sorted most-actionable first.
    ranks = [f["severity"] for f in body["findings"]]
    assert ranks == sorted(
        ranks,
        key=lambda s: (
            -{"critical": 4, "indeterminate": 3, "warning": 2, "info": 1, "ok": 0}.get(s, -1)
        ),
    )


def test_fetch_failure_becomes_indeterminate_not_500(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from api.services import monitoring as monitoring_svc
    from azure.core.exceptions import HttpResponseError

    def _boom(*a, **k):
        err = HttpResponseError("forbidden")
        err.status_code = 403
        raise err

    monkeypatch.setattr(monitoring_svc, "list_aks_clusters_detail_in_subscription", _boom)
    monkeypatch.setattr(monitoring_svc, "get_storage_account_detail", _boom)
    monkeypatch.setattr(monitoring_svc, "get_acr_registry_detail", _boom)

    resp = client.get(_url())
    assert resp.status_code == 200
    body = resp.json()
    severities = {f["severity"] for f in body["findings"]}
    assert "indeterminate" in severities
    assert "critical" not in severities  # permission-denied never escalates
    assert body["has_indeterminate"] is True


def test_requires_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AUTH_DEV_BYPASS", raising=False)
    monkeypatch.setenv("SIDECAR_REPORTER_DISABLED", "true")
    from api import main as api_main

    unauth = TestClient(api_main.create_app())
    resp = unauth.get("/api/diagnostics/reliability?subscription_id=sub-1")
    assert resp.status_code in (401, 403)


def test_availability_report_shape(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from api.services import monitoring as monitoring_svc

    monkeypatch.setattr(
        monitoring_svc,
        "list_aks_clusters_detail_in_subscription",
        lambda *a, **k: [],  # no clusters → info finding, exercises the path
    )

    resp = client.get(
        "/api/diagnostics/availability?subscription_id=sub-1&workload_resource_group=rg-elb"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["category"] == "availability"
    ids = {f["id"] for f in body["findings"]}
    # api + container_app are local reads, always present; aks present via the
    # no-clusters info finding.
    assert "aks.node_pressure" in ids


def test_reliability_and_security_share_one_fetch(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Performance contract: Reliability and Security use the same gatherer, so
    viewing both must hit ARM only once (shared snapshot cache, not per-category).
    """
    from api.services import monitoring as monitoring_svc

    calls = {"aks": 0, "storage": 0, "acr": 0}

    def _aks(*a, **k):
        calls["aks"] += 1
        return [
            {
                "name": "c1",
                "resource_group": "rg-elb",
                "provisioning_state": "Succeeded",
                "power_state": "Running",
                "k8s_version": "1.30.4",
                "agent_pools": [{"mode": "User", "enable_auto_scaling": True}],
                "aad_managed": True,
            }
        ]

    def _storage(*a, **k):
        calls["storage"] += 1
        return {"name": "stelb", "sku": "Standard_GRS"}

    def _acr(*a, **k):
        calls["acr"] += 1
        return {"name": "acrelb", "sku": "Premium"}

    monkeypatch.setattr(monitoring_svc, "list_aks_clusters_detail_in_subscription", _aks)
    monkeypatch.setattr(monitoring_svc, "get_storage_account_detail", _storage)
    monkeypatch.setattr(monitoring_svc, "get_acr_registry_detail", _acr)

    assert client.get(_url("reliability")).status_code == 200
    assert client.get(_url("security")).status_code == 200

    # One fetch total across both categories — the second served from the
    # shared snapshot cache.
    assert calls == {"aks": 1, "storage": 1, "acr": 1}

