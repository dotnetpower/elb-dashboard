from __future__ import annotations

import os
from typing import Any

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    os.environ.setdefault("AZURE_TENANT_ID", "common")
    os.environ.setdefault("API_CLIENT_ID", "00000000-0000-0000-0000-000000000000")
    from api.main import app

    return TestClient(app)


def test_warmup_start_forwards_cluster_topology_to_task(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []

    class FakeAsyncResult:
        id = "task-warmup-123"

    def fake_send_task(
        task_name: str,
        *,
        kwargs: dict[str, Any],
        queue: str | None = None,
    ) -> FakeAsyncResult:
        calls.append({"task_name": task_name, "queue": queue, **kwargs})
        return FakeAsyncResult()

    monkeypatch.setattr("api.celery_app.celery_app.send_task", fake_send_task)

    response = client.post(
        "/api/warmup/start",
        json={
            "subscription_id": "00000000-0000-0000-0000-000000000001",
            "resource_group": "rg-elb",
            "storage_account": "elbstg01",
            "db": "blast-db/core_nt",
            "program": "blastn",
            "aks_cluster_name": "aks-elb",
            "machine_type": "Standard_E16s_v5",
            "num_nodes": 10,
            "acr_name": "elbacr01",
        },
    )

    assert response.status_code == 200
    assert response.json()["instance_id"] == "task-warmup-123"
    assert calls[0]["task_name"] == "api.tasks.storage.warmup_database"
    assert calls[0]["queue"] == "storage"
    assert calls[0]["database_name"] == "core_nt"
    assert calls[0]["cluster_name"] == "aks-elb"
    assert calls[0]["machine_type"] == "Standard_E16s_v5"
    assert calls[0]["num_nodes"] == 10
    assert calls[0]["acr_name"] == "elbacr01"


def test_aks_start_forwards_auto_warmup_payload(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []

    class FakeAsyncResult:
        id = "task-start-aks"

    def fake_delay(**kwargs: Any) -> FakeAsyncResult:
        calls.append(kwargs)
        return FakeAsyncResult()

    monkeypatch.setattr("api.tasks.azure.start_aks.delay", fake_delay)

    response = client.post(
        "/api/aks/start",
        json={
            "subscription_id": "sub-1",
            "resource_group": "rg-elb",
            "cluster_name": "elb-cluster",
            "auto_warmup": {
                "storage_account": "elbstg01",
                "storage_resource_group": "rg-elb",
                "databases": ["core_nt"],
                "programs": {"core_nt": "blastn"},
                "enabled": True,
                "acr_resource_group": "rg-elbacr",
                "acr_name": "elbacr01",
            },
        },
    )

    assert response.status_code == 200
    assert response.json()["task_id"] == "task-start-aks"
    assert calls[0]["auto_warmup"]["databases"] == ["core_nt"]
    assert calls[0]["auto_warmup"]["storage_account"] == "elbstg01"
    assert calls[0]["auto_openapi"] == {
        "acr_name": "elbacr01",
        "acr_resource_group": "rg-elbacr",
        "storage_account": "elbstg01",
        "storage_resource_group": "rg-elb",
        "tenant_id": "common",
        "caller_oid": "00000000-0000-0000-0000-000000000000",
    }


def test_aks_start_forwards_auto_openapi_payload(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []

    class FakeAsyncResult:
        id = "task-start-aks"

    def fake_delay(**kwargs: Any) -> FakeAsyncResult:
        calls.append(kwargs)
        return FakeAsyncResult()

    monkeypatch.setattr("api.tasks.azure.start_aks.delay", fake_delay)

    response = client.post(
        "/api/aks/start",
        json={
            "subscription_id": "sub-1",
            "resource_group": "rg-elb",
            "cluster_name": "elb-cluster",
            "auto_openapi": {
                "acr_name": "elbacr01",
                "acr_resource_group": "rg-elbacr",
                "storage_account": "elbstg01",
                "storage_resource_group": "rg-storage",
            },
        },
    )

    assert response.status_code == 200
    assert response.json()["task_id"] == "task-start-aks"
    assert calls[0]["auto_openapi"] == {
        "acr_name": "elbacr01",
        "acr_resource_group": "rg-elbacr",
        "storage_account": "elbstg01",
        "storage_resource_group": "rg-storage",
        "tenant_id": "common",
        "caller_oid": "00000000-0000-0000-0000-000000000000",
    }


def test_aks_assign_roles_forwards_storage_rbac_fields(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []

    class FakeAsyncResult:
        id = "task-assign-roles"

    def fake_delay(**kwargs: Any) -> FakeAsyncResult:
        calls.append(kwargs)
        return FakeAsyncResult()

    monkeypatch.setattr("api.tasks.azure.assign_aks_roles.delay", fake_delay)

    response = client.post(
        "/api/aks/elb-cluster/assign-roles",
        json={
            "subscription_id": "sub-1",
            "resource_group": "rg-elb",
            "acr_resource_group": "rg-elbacr",
            "acr_name": "elbacr01",
            "storage_resource_group": "rg-elb-storage",
            "storage_account": "elbstg01",
        },
    )

    assert response.status_code == 200
    assert response.json()["task_id"] == "task-assign-roles"
    assert calls[0]["cluster_name"] == "elb-cluster"
    assert calls[0]["acr_resource_group"] == "rg-elbacr"
    assert calls[0]["storage_resource_group"] == "rg-elb-storage"
    assert calls[0]["storage_account"] == "elbstg01"


def test_warmup_auto_preference_round_trip(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    monkeypatch.setenv("ELB_LOCAL_STATE_DIR", str(tmp_path))

    response = client.put(
        "/api/warmup/auto-preference",
        json={
            "subscription_id": "sub-1",
            "resource_group": "rg-elb",
            "cluster_name": "elb-cluster",
            "storage_account": "elbstg01",
            "storage_resource_group": "rg-elb",
            "databases": ["blast-db/core_nt"],
            "programs": {"core_nt": "blastn"},
            "enabled": True,
        },
    )

    assert response.status_code == 200
    assert response.json()["preference"]["databases"] == ["core_nt"]

    get_response = client.get(
        "/api/warmup/auto-preference",
        params={
            "subscription_id": "sub-1",
            "resource_group": "rg-elb",
            "cluster_name": "elb-cluster",
        },
    )

    assert get_response.status_code == 200
    assert get_response.json()["preference"]["storage_account"] == "elbstg01"


def test_warmup_release_calls_k8s_helper(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_release(
        credential: object,
        subscription_id: str,
        resource_group: str,
        cluster_name: str,
        db_name: str,
    ) -> dict[str, Any]:
        calls.append(
            {
                "credential": credential,
                "subscription_id": subscription_id,
                "resource_group": resource_group,
                "cluster_name": cluster_name,
                "db_name": db_name,
            }
        )
        return {"status": "released", "database": db_name, "deleted": [], "errors": []}

    monkeypatch.setattr("api.services.get_credential", lambda: object())
    monkeypatch.setattr(
        "api.services.k8s_monitoring.k8s_release_warmup_cache",
        fake_release,
    )

    response = client.post(
        "/api/warmup/release",
        json={
            "subscription_id": "00000000-0000-0000-0000-000000000001",
            "resource_group": "rg-elb",
            "aks_cluster_name": "aks-elb",
            "db": "blast-db/core_nt",
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "released"
    assert calls[0]["db_name"] == "core_nt"
    assert calls[0]["cluster_name"] == "aks-elb"


def test_warmup_status_preserves_failed_task_payload(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeResult:
        status = "SUCCESS"

        def __init__(self, instance_id: str, app: object) -> None:
            self.instance_id = instance_id
            self.app = app
            self.result = {
                "status": "failed",
                "database": "core_nt",
                "error": "node warmup failed",
            }

        def ready(self) -> bool:
            return True

        def successful(self) -> bool:
            return True

    monkeypatch.setattr("celery.result.AsyncResult", FakeResult)

    response = client.get("/api/warmup/task-123/status")

    assert response.status_code == 200
    body = response.json()
    assert body["runtime_status"] == "Completed"
    assert body["output"]["status"] == "failed"
    assert body["output"]["db"] == "core_nt"
    assert body["output"]["error"] == "node warmup failed"
