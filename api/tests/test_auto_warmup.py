from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

from api.services.auto_warmup import (
    AutoWarmupPreference,
    get_auto_warmup_preference,
    save_auto_warmup_preference,
)
from api.tasks.storage import reconcile_auto_warmup


def test_reconcile_auto_warmup_enqueues_downloaded_db(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    monkeypatch.setenv("ELB_LOCAL_STATE_DIR", str(tmp_path))
    monkeypatch.setattr("api.tasks.storage.get_credential", lambda: object())
    monkeypatch.setattr(
        "api.services.monitoring.list_aks_clusters",
        lambda credential, subscription_id, resource_group: [
            {
                "name": "elb-cluster",
                "provisioning_state": "Succeeded",
                "power_state": "Running",
                "node_count": 10,
                "node_sku": "Standard_E16s_v5",
            }
        ],
    )
    monkeypatch.setattr(
        "api.services.monitoring.k8s_warmup_status",
        lambda credential, subscription_id, resource_group, cluster_name: {"databases": []},
    )
    monkeypatch.setattr(
        "api.services.storage_data.list_databases",
        lambda credential, storage_account: [{"name": "core_nt"}],
    )

    calls: list[dict[str, Any]] = []

    class FakeTask:
        id = "warmup-task-1"

    def fake_send_task(task_name: str, *, kwargs: dict[str, Any], queue: str) -> FakeTask:
        calls.append({"task_name": task_name, "kwargs": kwargs, "queue": queue})
        return FakeTask()

    monkeypatch.setattr("api.celery_app.celery_app.send_task", fake_send_task)

    pref = AutoWarmupPreference(
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="elb-cluster",
        storage_account="elbstg01",
        storage_resource_group="rg-elb",
        databases=["core_nt"],
        programs={"core_nt": "blastn"},
        acr_name="elbacr01",
    )

    result = reconcile_auto_warmup.run(preference=pref.to_dict(), force=True)

    assert result["status"] == "completed"
    assert result["clusters"][0]["status"] == "triggered"
    assert result["clusters"][0]["enqueued"] == [{"db": "core_nt", "task_id": "warmup-task-1"}]
    assert calls[0]["task_name"] == "api.tasks.storage.warmup_database"
    assert calls[0]["queue"] == "storage"
    assert calls[0]["kwargs"]["database_name"] == "core_nt"
    assert calls[0]["kwargs"]["machine_type"] == "Standard_E16s_v5"
    assert calls[0]["kwargs"]["num_nodes"] == 10


def test_reconcile_auto_warmup_skips_until_cluster_ready(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    monkeypatch.setenv("ELB_LOCAL_STATE_DIR", str(tmp_path))
    monkeypatch.setattr("api.tasks.storage.get_credential", lambda: object())
    monkeypatch.setattr(
        "api.services.monitoring.list_aks_clusters",
        lambda credential, subscription_id, resource_group: [
            {
                "name": "elb-cluster",
                "provisioning_state": "Succeeded",
                "power_state": "Stopped",
                "node_count": 10,
                "node_sku": "Standard_E16s_v5",
            }
        ],
    )

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "api.celery_app.celery_app.send_task",
        lambda *args, **kwargs: calls.append({"args": args, "kwargs": kwargs}),
    )

    pref = AutoWarmupPreference(
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="elb-cluster",
        storage_account="elbstg01",
        storage_resource_group="rg-elb",
        databases=["core_nt"],
    )

    result = reconcile_auto_warmup.run(preference=pref.to_dict(), force=True)

    assert result["clusters"][0]["status"] == "not_ready"
    assert calls == []


def test_auto_warmup_file_store_handles_concurrent_saves(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    monkeypatch.setenv("ELB_LOCAL_STATE_DIR", str(tmp_path))

    def save(index: int) -> None:
        save_auto_warmup_preference(
            AutoWarmupPreference(
                subscription_id="sub-1",
                resource_group="rg-elb",
                cluster_name=f"cluster-{index}",
                storage_account="elbstg01",
                storage_resource_group="rg-elb",
                databases=["core_nt"],
            )
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(save, range(16)))

    for index in range(16):
        pref = get_auto_warmup_preference("sub-1", "rg-elb", f"cluster-{index}")
        assert pref is not None
        assert pref.databases == ["core_nt"]
