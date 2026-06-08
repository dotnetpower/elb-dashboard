"""Tests for Warmup Route behavior.

Responsibility: Tests for Warmup Route behavior
Edit boundaries: Keep assertions focused on the behavior under test; prefer fakes over live
Azure calls.
Key entry points: `client`, `test_warmup_start_forwards_cluster_topology_to_task`,
`test_aks_start_forwards_auto_warmup_payload`, `test_aks_start_forwards_auto_openapi_payload`,
`test_aks_assign_roles_forwards_storage_rbac_fields`,
`test_aks_lifecycle_routes_invalidate_monitor_cache`
Risky contracts: Do not require network access or real Azure credentials unless the test is
explicitly integration-scoped.
Validation: `uv run pytest -q api/tests/test_warmup_route.py`.
"""

from __future__ import annotations

from typing import Any

import pytest
from api.tests._fakes import (
    AsyncResultStub,
    make_delay_recorder,
    make_send_task_recorder,
)
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setenv("AZURE_TENANT_ID", "common")
    monkeypatch.setenv("API_CLIENT_ID", "00000000-0000-0000-0000-000000000000")
    from api.main import app

    return TestClient(app)


def test_warmup_start_forwards_cluster_topology_to_task(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls, fake_send_task = make_send_task_recorder("task-warmup-123")

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
    assert calls[0]["kwargs"]["database_name"] == "core_nt"
    assert calls[0]["kwargs"]["cluster_name"] == "aks-elb"
    assert calls[0]["kwargs"]["machine_type"] == "Standard_E16s_v5"
    assert calls[0]["kwargs"]["num_nodes"] == 10
    assert calls[0]["kwargs"]["acr_name"] == "elbacr01"


def test_aks_start_forwards_auto_warmup_payload(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls, fake_delay = make_delay_recorder("task-start-aks")

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


def test_aks_scale_forwards_node_count_and_warmup(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls, fake_delay = make_delay_recorder("task-scale-aks")

    monkeypatch.setattr("api.tasks.azure.scale_aks.delay", fake_delay)

    response = client.post(
        "/api/aks/scale",
        json={
            "subscription_id": "sub-1",
            "resource_group": "rg-elb",
            "cluster_name": "elb-cluster",
            "node_count": 5,
            "auto_warmup": {
                "storage_account": "elbstg01",
                "databases": ["core_nt"],
                "enabled": True,
            },
        },
    )

    assert response.status_code == 200
    assert response.json()["task_id"] == "task-scale-aks"
    assert calls[0]["node_count"] == 5
    assert calls[0]["cluster_name"] == "elb-cluster"
    assert calls[0]["auto_warmup"]["databases"] == ["core_nt"]


@pytest.mark.parametrize("bad_count", [0, -1, 9999, "abc", None])
def test_aks_scale_rejects_invalid_node_count(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, bad_count: object
) -> None:
    calls, fake_delay = make_delay_recorder("should-not-run")
    monkeypatch.setattr("api.tasks.azure.scale_aks.delay", fake_delay)

    response = client.post(
        "/api/aks/scale",
        json={
            "subscription_id": "sub-1",
            "resource_group": "rg-elb",
            "cluster_name": "elb-cluster",
            "node_count": bad_count,
        },
    )

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_node_count"
    assert calls == [], "task must not be enqueued for an invalid node_count"


def test_aks_start_forwards_auto_openapi_payload(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls, fake_delay = make_delay_recorder("task-start-aks")

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
    calls, fake_delay = make_delay_recorder("task-assign-roles")

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


@pytest.mark.parametrize(
    "verb,task_attr",
    [
        ("start", "start_aks"),
        ("stop", "stop_aks"),
        ("delete", "delete_aks"),
    ],
)
def test_aks_lifecycle_routes_invalidate_monitor_cache(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    verb: str,
    task_attr: str,
) -> None:
    """start/stop/delete must drop every monitor:aks:* cache key for the targeted scope.

    Regression guard: this is the load-bearing fix for the "cluster shows
    Stopped even after Start" stale-cache bug. If a new monitor:aks:<x>
    cache key is added under api/routes/monitor/, this test must be
    updated alongside `api.routes.aks._invalidate_aks_monitor_cache`.
    """
    from api.services import monitor_cache

    monkeypatch.setattr(
        f"api.tasks.azure.{task_attr}.delay",
        lambda **_: AsyncResultStub(f"task-{verb}"),
    )

    sub = "sub-cache-1"
    rg = "rg-elb"
    cluster = "elb-cluster"
    monitor_cache.reset_monitor_snapshot_cache()
    # Seed every monitor:aks:* key shape we currently produce.
    seeded = [
        f"monitor:aks:{sub}:{rg}",
        f"monitor:aks:nodes:{sub}:{rg}:{cluster}",
        f"monitor:aks:pods:{sub}:{rg}:{cluster}",
        f"monitor:aks:top-nodes:{sub}:{rg}:{cluster}",
        f"monitor:aks:warmup-status:{sub}:{rg}:{cluster}",
        f"monitor:aks:events:{sub}:{rg}:{cluster}:default:50",
        # Pillar B (2026-05-27): per-cluster ARM `power_state` snapshot
        # consumed by `cluster_health.get_cluster_health`. MUST be cleared
        # on lifecycle mutations or the gate keeps the previous reading
        # for up to 90 s and either skips healthy K8s polls right after
        # Start (false `cluster_stopped` chip) or re-introduces K8s
        # connect-timeout noise right after Stop.
        f"monitor:aks:meta:{sub}:{rg}:{cluster}",
    ]
    for key in seeded:
        monitor_cache.cached_snapshot(key, lambda: {"seeded": True}, ttl_seconds=30)
    # Neighbour key that shares a string prefix but a different RG must survive.
    monitor_cache.cached_snapshot(
        f"monitor:aks:{sub}:{rg}-suffix",
        lambda: {"different_rg": True},
        ttl_seconds=30,
    )
    # Different namespace (storage) must also survive.
    monitor_cache.cached_snapshot(
        f"monitor:storage:{sub}:{rg}:acct",
        lambda: {"storage": True},
        ttl_seconds=30,
    )

    body = {"subscription_id": sub, "resource_group": rg, "cluster_name": cluster}
    response = client.post(f"/api/aks/{verb}", json=body)
    assert response.status_code == 200

    # Every seeded key under the targeted scope must be gone — the next monitor
    # poll has to hit ARM, not the previous "Stopped" snapshot.
    for key in seeded:
        reload_calls = 0

        def loader() -> dict[str, Any]:
            nonlocal reload_calls
            reload_calls += 1
            return {"reloaded": True}

        result = monitor_cache.cached_snapshot(key, loader, ttl_seconds=30)
        assert reload_calls == 1, f"{key} was NOT invalidated"
        assert result["cache"]["state"] == "refreshed"

    # Sibling scopes were preserved.
    survivor = monitor_cache.cached_snapshot(
        f"monitor:aks:{sub}:{rg}-suffix",
        lambda: {"should_not_run": True},
        ttl_seconds=30,
    )
    assert survivor["cache"]["state"] == "fresh"
    assert survivor["different_rg"] is True

    storage = monitor_cache.cached_snapshot(
        f"monitor:storage:{sub}:{rg}:acct",
        lambda: {"should_not_run": True},
        ttl_seconds=30,
    )
    assert storage["cache"]["state"] == "fresh"
    assert storage["storage"] is True


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


def test_auto_preference_put_preserves_pending_force_rewarm(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The SPA auto-sync PUT (DB list / node-count change) must NOT clear a
    `force_rewarm_pending` that start_aks/scale_aks just set. Otherwise the
    node-count scale's auto re-warm is silently dropped (2026-06-08 live E2E)."""
    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    monkeypatch.setenv("ELB_LOCAL_STATE_DIR", str(tmp_path))

    from api.services.auto_warmup import (
        get_auto_warmup_preference,
        normalise_preference,
        save_auto_warmup_preference,
    )

    # scale_aks persisted a pending forced re-warm pinned to the new size.
    save_auto_warmup_preference(
        normalise_preference(
            {
                "subscription_id": "sub-1",
                "resource_group": "rg-elb",
                "cluster_name": "elb-cluster",
                "storage_account": "elbstg01",
                "storage_resource_group": "rg-elb",
                "databases": ["16S_ribosomal_RNA"],
                "enabled": True,
                "num_nodes": 5,
                "force_rewarm_pending": True,
            }
        )
    )

    # SPA auto-sync fires after node_count flips 10->5; body omits the flag.
    response = client.put(
        "/api/warmup/auto-preference",
        json={
            "subscription_id": "sub-1",
            "resource_group": "rg-elb",
            "cluster_name": "elb-cluster",
            "storage_account": "elbstg01",
            "storage_resource_group": "rg-elb",
            "databases": ["core_nt"],
            "enabled": True,
            "num_nodes": 5,
        },
    )
    assert response.status_code == 200
    # The pending forced re-warm survives the clobbering upsert.
    assert response.json()["preference"]["force_rewarm_pending"] is True
    persisted = get_auto_warmup_preference("sub-1", "rg-elb", "elb-cluster")
    assert persisted is not None
    assert persisted.force_rewarm_pending is True


def test_auto_preference_put_honours_explicit_force_clear(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """An explicit `force_rewarm_pending=false` in the body still wins (the
    reconcile's own bookkeeping path must be able to clear it)."""
    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    monkeypatch.setenv("ELB_LOCAL_STATE_DIR", str(tmp_path))

    from api.services.auto_warmup import (
        get_auto_warmup_preference,
        normalise_preference,
        save_auto_warmup_preference,
    )

    save_auto_warmup_preference(
        normalise_preference(
            {
                "subscription_id": "sub-1",
                "resource_group": "rg-elb",
                "cluster_name": "elb-cluster",
                "storage_account": "elbstg01",
                "storage_resource_group": "rg-elb",
                "databases": ["core_nt"],
                "enabled": True,
                "force_rewarm_pending": True,
            }
        )
    )

    response = client.put(
        "/api/warmup/auto-preference",
        json={
            "subscription_id": "sub-1",
            "resource_group": "rg-elb",
            "cluster_name": "elb-cluster",
            "storage_account": "elbstg01",
            "storage_resource_group": "rg-elb",
            "databases": ["core_nt"],
            "enabled": True,
            "force_rewarm_pending": False,
        },
    )
    assert response.status_code == 200
    assert response.json()["preference"]["force_rewarm_pending"] is False
    persisted = get_auto_warmup_preference("sub-1", "rg-elb", "elb-cluster")
    assert persisted is not None
    assert persisted.force_rewarm_pending is False


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
        "api.services.k8s.monitoring.k8s_release_warmup_cache",
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
