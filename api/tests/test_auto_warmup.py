"""Tests for Auto Warmup behavior.

Responsibility: Tests for Auto Warmup behavior
Edit boundaries: Keep assertions focused on the behavior under test; prefer fakes over live
Azure calls.
Key entry points: `_patch_ready_warmup_nodes`,
`test_reconcile_auto_warmup_enqueues_downloaded_db`,
`test_reconcile_auto_warmup_waits_for_all_ready_workload_nodes`,
`test_reconcile_auto_warmup_enqueues_when_all_ready_workload_nodes`,
`test_reconcile_auto_warmup_skips_stale_downloaded_generation`,
`test_reconcile_auto_warmup_reenqueues_stale_warm_generation`
Risky contracts: Do not require network access or real Azure credentials unless the test is
explicitly integration-scoped.
Validation: `uv run pytest -q api/tests/test_auto_warmup.py`.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

from api.services import auto_warmup
from api.services.auto_warmup import (
    AutoWarmupPreference,
    get_auto_warmup_preference,
    save_auto_warmup_preference,
)
from api.tasks.storage import reconcile_auto_warmup, warmup_database
from api.tests._fakes import make_send_task_recorder


def _patch_ready_warmup_nodes(monkeypatch, count: int) -> None:
    monkeypatch.setattr(
        "api.services.k8s.monitoring.k8s_ready_warmup_node_names",
        lambda credential, subscription_id, resource_group, cluster_name: [
            f"aks-blast-{index:06d}" for index in range(count)
        ],
    )


def test_reconcile_auto_warmup_enqueues_downloaded_db(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    monkeypatch.setenv("ELB_LOCAL_STATE_DIR", str(tmp_path))
    monkeypatch.setattr("api.tasks.storage.get_credential", lambda: object())
    monkeypatch.setattr(
        "api.tasks.storage._autowarmup_inflight_acquire",
        lambda *args, **kwargs: True,
    )
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
    _patch_ready_warmup_nodes(monkeypatch, 10)
    monkeypatch.setattr(
        "api.services.monitoring.k8s_warmup_status",
        lambda credential, subscription_id, resource_group, cluster_name: {"databases": []},
    )
    monkeypatch.setattr(
        "api.services.storage.data.list_databases",
        lambda credential, storage_account: [{"name": "core_nt"}],
    )

    calls, fake_send_task = make_send_task_recorder("warmup-task-1")

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
    assert calls[0]["kwargs"]["require_all_warmup_nodes"] is True
    # Regression: the Storage account's RG must be forwarded explicitly. The
    # reconciler historically omitted this kwarg, which made warmup_database
    # fall back to the AKS cluster RG and silently skip the RBAC ensure.
    assert calls[0]["kwargs"]["storage_resource_group"] == "rg-elb"


def test_reconcile_auto_warmup_seeds_job_state_before_enqueue(
    monkeypatch,
    tmp_path,
) -> None:
    """Auto-warmup reconciler must seed JobState before enqueueing the task.

    Without the seed, `warmup_database`'s first `_update_state` checkpoint
    calls `repo.update()` → `get_entity` → 404, which surfaces as a red
    Dependency failure in App Insights and silently drops every phase
    update so the SPA can't render progress.
    """

    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    monkeypatch.setenv("ELB_LOCAL_STATE_DIR", str(tmp_path))
    monkeypatch.setattr("api.tasks.storage.get_credential", lambda: object())
    monkeypatch.setattr(
        "api.tasks.storage._autowarmup_inflight_acquire",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        "api.services.monitoring.list_aks_clusters",
        lambda credential, subscription_id, resource_group: [
            {
                "name": "elb-cluster",
                "provisioning_state": "Succeeded",
                "power_state": "Running",
                "node_count": 4,
                "node_sku": "Standard_E16s_v5",
            }
        ],
    )
    _patch_ready_warmup_nodes(monkeypatch, 4)
    monkeypatch.setattr(
        "api.services.monitoring.k8s_warmup_status",
        lambda credential, subscription_id, resource_group, cluster_name: {"databases": []},
    )
    monkeypatch.setattr(
        "api.services.storage.data.list_databases",
        lambda credential, storage_account: [{"name": "core_nt"}],
    )

    created: list[Any] = []
    updates: list[dict[str, Any]] = []

    class _FakeRepo:
        def create(self, state: Any) -> Any:
            created.append(state)
            return state

        def update(self, job_id: str, **kwargs: Any) -> Any:
            updates.append({"job_id": job_id, **kwargs})
            return object()

    fake_repo = _FakeRepo()
    monkeypatch.setattr("api.services.state_repo.get_state_repo", lambda: fake_repo)

    calls, fake_send_task = make_send_task_recorder("warmup-task-seeded")
    monkeypatch.setattr("api.celery_app.celery_app.send_task", fake_send_task)

    pref = AutoWarmupPreference(
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="elb-cluster",
        storage_account="elbstg01",
        storage_resource_group="rg-elb",
        databases=["core_nt"],
        programs={"core_nt": "blastn"},
        owner_oid="auto-warmup-owner",
    )

    result = reconcile_auto_warmup.run(preference=pref.to_dict(), force=True)
    assert result["clusters"][0]["status"] == "triggered"
    assert calls and calls[0]["task_name"] == "api.tasks.storage.warmup_database"

    job_id = calls[0]["kwargs"]["job_id"]
    assert job_id.startswith("auto-warmup-elb-cluster-core_nt-")

    # JobState row must be created before send_task, with the canonical fields
    # the warmup task and SPA expect.
    assert len(created) == 1, "JobState.create() must be called exactly once before enqueue"
    seeded = created[0]
    assert seeded.job_id == job_id
    assert seeded.type == "warmup"
    assert seeded.status == "queued"
    assert seeded.phase == "queued"
    assert seeded.db == "core_nt"
    assert seeded.program == "blastn"
    assert seeded.cluster_name == "elb-cluster"
    assert seeded.owner_oid == "auto-warmup-owner"

    # task_id must be attached after enqueue so the SPA / status routes can
    # resolve the Celery task from the job row.
    assert any(
        u["job_id"] == job_id and u.get("task_id") == "warmup-task-seeded" for u in updates
    ), f"task_id must be attached to seeded JobState; updates={updates}"





def test_reconcile_auto_warmup_waits_for_all_ready_workload_nodes(
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
    _patch_ready_warmup_nodes(monkeypatch, 8)

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
        num_nodes=10,
    )

    result = reconcile_auto_warmup.run(preference=pref.to_dict(), force=True)

    cluster_result = result["clusters"][0]
    assert cluster_result["status"] == "waiting_for_warmup_nodes"
    assert cluster_result["phase"] == "waiting_for_warmup_nodes"
    assert cluster_result["reason"] == "waiting for all warmup nodes"
    assert cluster_result["expected_node_count"] == 10
    assert cluster_result["ready_node_count"] == 8
    assert cluster_result["skipped"] == [
        {
            "reason": "waiting_for_all_warmup_nodes",
            "expected_node_count": 10,
            "ready_node_count": 8,
        }
    ]
    assert calls == []


def test_reconcile_auto_warmup_enqueues_when_all_ready_workload_nodes(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    monkeypatch.setenv("ELB_LOCAL_STATE_DIR", str(tmp_path))
    monkeypatch.setattr("api.tasks.storage.get_credential", lambda: object())
    monkeypatch.setattr(
        "api.tasks.storage._autowarmup_inflight_acquire",
        lambda *args, **kwargs: True,
    )
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
    _patch_ready_warmup_nodes(monkeypatch, 10)
    monkeypatch.setattr(
        "api.services.monitoring.k8s_warmup_status",
        lambda credential, subscription_id, resource_group, cluster_name: {"databases": []},
    )
    monkeypatch.setattr(
        "api.services.storage.data.list_databases",
        lambda credential, storage_account: [{"name": "core_nt"}],
    )

    calls, fake_send_task = make_send_task_recorder("warmup-task-ready")

    monkeypatch.setattr("api.celery_app.celery_app.send_task", fake_send_task)

    pref = AutoWarmupPreference(
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="elb-cluster",
        storage_account="elbstg01",
        storage_resource_group="rg-elb",
        databases=["core_nt"],
        num_nodes=10,
    )

    result = reconcile_auto_warmup.run(preference=pref.to_dict(), force=True)

    assert result["clusters"][0]["status"] == "triggered"
    assert result["clusters"][0]["enqueued"] == [{"db": "core_nt", "task_id": "warmup-task-ready"}]
    assert calls[0]["kwargs"]["num_nodes"] == 10
    assert calls[0]["kwargs"]["require_all_warmup_nodes"] is True


def test_reconcile_auto_warmup_skips_stale_downloaded_generation(
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
    _patch_ready_warmup_nodes(monkeypatch, 10)
    monkeypatch.setattr(
        "api.services.monitoring.k8s_warmup_status",
        lambda credential, subscription_id, resource_group, cluster_name: {"databases": []},
    )
    monkeypatch.setattr(
        "api.services.storage.data.list_databases",
        lambda credential, storage_account: [
            {"name": "core_nt", "source_version": "2026-05-19-00-00-00"}
        ],
    )
    monkeypatch.setattr(
        "api.routes.storage.common._resolve_latest_dir",
        lambda: "2026-05-20-00-00-00",
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

    assert result["clusters"][0]["status"] == "ready_noop"
    assert result["clusters"][0]["skipped"] == [
        {
            "db": "core_nt",
            "reason": "update_required",
            "source_version": "2026-05-19-00-00-00",
            "latest_version": "2026-05-20-00-00-00",
        }
    ]
    assert calls == []


def test_reconcile_auto_warmup_reenqueues_stale_warm_generation(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    monkeypatch.setenv("ELB_LOCAL_STATE_DIR", str(tmp_path))
    monkeypatch.setattr("api.tasks.storage.get_credential", lambda: object())
    monkeypatch.setattr(
        "api.tasks.storage._autowarmup_inflight_acquire",
        lambda *args, **kwargs: True,
    )
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
    _patch_ready_warmup_nodes(monkeypatch, 10)
    monkeypatch.setattr(
        "api.services.monitoring.k8s_warmup_status",
        lambda credential, subscription_id, resource_group, cluster_name: {
            "databases": [
                {
                    "name": "core_nt",
                    "status": "Ready",
                    "source_version": "2026-05-19-00-00-00",
                }
            ]
        },
    )
    monkeypatch.setattr(
        "api.services.storage.data.list_databases",
        lambda credential, storage_account: [
            {"name": "core_nt", "source_version": "2026-05-20-00-00-00"}
        ],
    )
    monkeypatch.setattr(
        "api.routes.storage.common._resolve_latest_dir",
        lambda: "2026-05-20-00-00-00",
    )

    calls, fake_send_task = make_send_task_recorder("warmup-task-current-generation")

    monkeypatch.setattr("api.celery_app.celery_app.send_task", fake_send_task)

    pref = AutoWarmupPreference(
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="elb-cluster",
        storage_account="elbstg01",
        storage_resource_group="rg-elb",
        databases=["core_nt"],
    )

    result = reconcile_auto_warmup.run(preference=pref.to_dict(), force=True)

    assert result["clusters"][0]["status"] == "triggered"
    assert result["clusters"][0]["enqueued"] == [
        {"db": "core_nt", "task_id": "warmup-task-current-generation"}
    ]
    assert calls[0]["kwargs"]["database_name"] == "core_nt"


def _patch_ready_db_warm_status(
    monkeypatch, *, source_version: str, warm_status: str = "Ready"
) -> None:
    """Patch the reconcile reads so a downloaded DB reports a matching warm Job.

    Models the post `az aks stop`/`start` state on a `node_disk` cluster: the
    pre-stop warmup Jobs survive (node names stay stable) so the DB reports
    ``Ready`` with the same generation as storage. There is no pending NCBI
    update, so the only thing standing between the reconcile and a re-warm is
    the ``Ready`` skip. ``warm_status`` lets a test model a lingering
    ``Failed`` Job instead.
    """

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
    _patch_ready_warmup_nodes(monkeypatch, 10)
    monkeypatch.setattr(
        "api.services.monitoring.k8s_warmup_status",
        lambda credential, subscription_id, resource_group, cluster_name: {
            "databases": [
                {
                    "name": "core_nt",
                    "status": warm_status,
                    "source_version": source_version,
                }
            ]
        },
    )
    monkeypatch.setattr(
        "api.services.storage.data.list_databases",
        lambda credential, storage_account: [
            {"name": "core_nt", "source_version": source_version}
        ],
    )
    monkeypatch.setattr(
        "api.routes.storage.common._resolve_latest_dir",
        lambda: source_version,
    )


def test_reconcile_auto_warmup_force_reenqueues_ready_db(monkeypatch, tmp_path) -> None:
    """A forced (post stop/start) reconcile must re-warm a DB that still
    reports ``Ready``.

    Root cause this guards: on a ``node_disk`` cluster the Managed OS disk
    keeps VMSS instance names stable across `az aks stop`/`start`, so the
    pre-stop warmup Jobs are not flagged ``Stale`` and the DB reports
    ``Ready`` even though the node RAM page cache is cold. ``start_aks``
    enqueues this reconcile with ``force=True`` to re-warm — which only works
    if ``force`` actually bypasses the ``Ready`` skip and the task is told to
    drop the stale Jobs (``force_rewarm``).
    """

    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    monkeypatch.setenv("ELB_LOCAL_STATE_DIR", str(tmp_path))
    monkeypatch.setattr("api.tasks.storage.get_credential", lambda: object())
    monkeypatch.setattr(
        "api.tasks.storage._autowarmup_inflight_acquire",
        lambda *args, **kwargs: True,
    )
    _patch_ready_db_warm_status(monkeypatch, source_version="2026-05-20-00-00-00")

    calls, fake_send_task = make_send_task_recorder("warmup-task-force")
    monkeypatch.setattr("api.celery_app.celery_app.send_task", fake_send_task)

    pref = AutoWarmupPreference(
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="elb-cluster",
        storage_account="elbstg01",
        storage_resource_group="rg-elb",
        databases=["core_nt"],
    )

    result = reconcile_auto_warmup.run(preference=pref.to_dict(), force=True)

    assert result["clusters"][0]["status"] == "triggered"
    assert result["clusters"][0]["enqueued"] == [{"db": "core_nt", "task_id": "warmup-task-force"}]
    # The task must be told this is a forced re-warm so it drops the lingering
    # Jobs before ensure (otherwise ensure no-ops on the existing names).
    assert calls[0]["kwargs"]["force_rewarm"] is True


def test_reconcile_auto_warmup_skips_ready_db_without_force(monkeypatch, tmp_path) -> None:
    """The periodic (un-forced) reconcile must keep skipping a warm DB.

    Counterpart to the forced re-warm: a routine beat tick that finds the DB
    already ``Ready`` with a matching generation must not re-enqueue, so the
    `force` bypass stays scoped to the post stop/start path.
    """

    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    monkeypatch.setenv("ELB_LOCAL_STATE_DIR", str(tmp_path))
    monkeypatch.setattr("api.tasks.storage.get_credential", lambda: object())
    monkeypatch.setattr(
        "api.tasks.storage._autowarmup_inflight_acquire",
        lambda *args, **kwargs: True,
    )
    _patch_ready_db_warm_status(monkeypatch, source_version="2026-05-20-00-00-00")

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

    result = reconcile_auto_warmup.run(preference=pref.to_dict())

    assert result["clusters"][0]["status"] == "ready_noop"
    assert result["clusters"][0]["skipped"] == [{"db": "core_nt", "reason": "Ready"}]
    assert calls == []


def test_reconcile_auto_warmup_force_rewarms_failed_db_without_force_flag(
    monkeypatch, tmp_path
) -> None:
    """A lingering ``Failed`` warmup Job must be force-released even on a
    periodic (un-forced) reconcile.

    On a ``node_disk`` cluster the Failed Jobs are pinned to LIVE nodes with
    stable names, so the node-staleness sweep keeps them and
    ``k8s_ensure_job_manifests`` would skip recreating them forever — the DB
    would stay ``Failed`` and the reconcile would busy-loop every beat tick
    without ever converging. The reconcile must therefore set
    ``force_rewarm=True`` for a Failed DB regardless of the ``force`` flag.
    """

    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    monkeypatch.setenv("ELB_LOCAL_STATE_DIR", str(tmp_path))
    monkeypatch.setattr("api.tasks.storage.get_credential", lambda: object())
    monkeypatch.setattr(
        "api.tasks.storage._autowarmup_inflight_acquire",
        lambda *args, **kwargs: True,
    )
    _patch_ready_db_warm_status(
        monkeypatch, source_version="2026-05-20-00-00-00", warm_status="Failed"
    )

    calls, fake_send_task = make_send_task_recorder("warmup-task-failed-recover")
    monkeypatch.setattr("api.celery_app.celery_app.send_task", fake_send_task)

    pref = AutoWarmupPreference(
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="elb-cluster",
        storage_account="elbstg01",
        storage_resource_group="rg-elb",
        databases=["core_nt"],
    )

    # Note: force is NOT passed — this is the periodic beat path.
    result = reconcile_auto_warmup.run(preference=pref.to_dict())

    assert result["clusters"][0]["status"] == "triggered"
    assert result["clusters"][0]["enqueued"] == [
        {"db": "core_nt", "task_id": "warmup-task-failed-recover"}
    ]
    # The Failed Jobs must be force-released so the retry actually re-runs.
    assert calls[0]["kwargs"]["force_rewarm"] is True

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


def test_reconcile_auto_warmup_reenqueues_when_db_not_warm(monkeypatch, tmp_path) -> None:
    """Regression: even when ``last_ready`` is already True from a prior tick,
    a downloaded DB that is not warm on the cluster must still get a warmup
    task enqueued. Previously a ``last_ready`` short-circuit returned
    ``already_ready`` and left newly downloaded DBs cold forever.
    """
    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    monkeypatch.setenv("ELB_LOCAL_STATE_DIR", str(tmp_path))
    monkeypatch.setattr("api.tasks.storage.get_credential", lambda: object())
    monkeypatch.setattr(
        "api.tasks.storage._autowarmup_inflight_acquire",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        "api.services.monitoring.list_aks_clusters",
        lambda credential, subscription_id, resource_group: [
            {
                "name": "elb-cluster",
                "provisioning_state": "Succeeded",
                "power_state": "Running",
                "node_count": 4,
                "node_sku": "Standard_E16s_v5",
            }
        ],
    )
    _patch_ready_warmup_nodes(monkeypatch, 4)
    monkeypatch.setattr(
        "api.services.monitoring.k8s_warmup_status",
        lambda credential, subscription_id, resource_group, cluster_name: {"databases": []},
    )
    monkeypatch.setattr(
        "api.services.storage.data.list_databases",
        lambda credential, storage_account: [{"name": "core_nt"}],
    )

    calls, fake_send_task = make_send_task_recorder("warmup-task-reenqueue")

    monkeypatch.setattr("api.celery_app.celery_app.send_task", fake_send_task)

    # Persist a preference with last_ready=True to simulate "cluster was ready
    # on a previous reconcile tick and core_nt was claimed warm at the time".
    pref = AutoWarmupPreference(
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="elb-cluster",
        storage_account="elbstg01",
        storage_resource_group="rg-elb",
        databases=["core_nt"],
        last_ready=True,
    )
    save_auto_warmup_preference(pref)

    # Note: no ``force`` flag — the beat schedule calls reconcile without it.
    result = reconcile_auto_warmup.run()

    assert result["clusters"][0]["status"] == "triggered"
    assert calls[0]["kwargs"]["database_name"] == "core_nt"


def test_reconcile_auto_warmup_inflight_lock_prevents_duplicate(monkeypatch, tmp_path) -> None:
    """A DB whose previous warmup task is still in its pre-Kubernetes phases
    (download / shard / plan) must not be re-enqueued by the next reconcile
    tick, because k8s_warmup_status cannot yet observe the pod.
    """
    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    monkeypatch.setenv("ELB_LOCAL_STATE_DIR", str(tmp_path))
    monkeypatch.setattr("api.tasks.storage.get_credential", lambda: object())
    monkeypatch.setattr(
        "api.tasks.storage._autowarmup_inflight_acquire",
        lambda *args, **kwargs: False,
    )
    monkeypatch.setattr(
        "api.services.monitoring.list_aks_clusters",
        lambda credential, subscription_id, resource_group: [
            {
                "name": "elb-cluster",
                "provisioning_state": "Succeeded",
                "power_state": "Running",
                "node_count": 4,
                "node_sku": "Standard_E16s_v5",
            }
        ],
    )
    _patch_ready_warmup_nodes(monkeypatch, 4)
    monkeypatch.setattr(
        "api.services.monitoring.k8s_warmup_status",
        lambda credential, subscription_id, resource_group, cluster_name: {"databases": []},
    )
    monkeypatch.setattr(
        "api.services.storage.data.list_databases",
        lambda credential, storage_account: [{"name": "core_nt"}],
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

    result = reconcile_auto_warmup.run(preference=pref.to_dict())

    assert result["clusters"][0]["status"] == "ready_noop"
    assert result["clusters"][0]["skipped"] == [{"db": "core_nt", "reason": "inflight"}]
    assert calls == []


def test_warmup_database_auto_strict_waits_for_requested_ready_nodes(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    monkeypatch.setenv("ELB_LOCAL_STATE_DIR", str(tmp_path))
    monkeypatch.setattr("api.tasks.storage.get_credential", lambda: object())
    monkeypatch.setattr(
        "api.services.storage.data.list_databases",
        lambda credential, storage_account: [
            {
                "name": "core_nt",
                "file_count": 12,
                "sharded": True,
                "shard_sets": [8, 10],
                "total_bytes": 1024,
            }
        ],
    )
    _patch_ready_warmup_nodes(monkeypatch, 8)

    build_calls: list[dict[str, Any]] = []

    def fake_build_warmup_job_plan(**kwargs: Any) -> object:
        build_calls.append(kwargs)
        raise AssertionError("build_warmup_job_plan should not be called")

    monkeypatch.setattr(
        "api.services.warmup.jobs.build_warmup_job_plan",
        fake_build_warmup_job_plan,
    )

    state_updates: list[dict[str, Any]] = []
    task_progress: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "api.tasks.storage._update_state",
        lambda job_id, phase, status="running", **extra: state_updates.append(
            {"job_id": job_id, "phase": phase, "status": status, **extra}
        ),
    )
    monkeypatch.setattr(
        "api.tasks.storage._record_task_progress",
        lambda task, phase, **meta: task_progress.append({"phase": phase, **meta}),
    )

    result = warmup_database.run(
        job_id="auto-warmup-elb-cluster-core_nt-1",
        subscription_id="sub-1",
        resource_group="rg-elb",
        storage_account="elbstg01",
        database_name="core_nt",
        storage_resource_group="rg-elb",
        cluster_name="elb-cluster",
        machine_type="Standard_E16s_v5",
        num_nodes=10,
        acr_name="elbacr01",
        require_all_warmup_nodes=True,
    )

    assert result["status"] == "deferred"
    assert result["phase"] == "waiting_for_warmup_nodes"
    assert result["reason"] == "waiting for all warmup nodes"
    assert result["node_warmup"]["requested_node_count"] == 10
    assert result["node_warmup"]["ready_node_count"] == 8
    assert build_calls == []
    assert any(update["phase"] == "waiting_for_warmup_nodes" for update in state_updates)
    assert any(progress["phase"] == "waiting_for_warmup_nodes" for progress in task_progress)


def test_warmup_database_force_rewarm_drops_existing_jobs(monkeypatch, tmp_path) -> None:
    """A forced re-warm must drop the database's existing warmup Jobs first.

    On a ``node_disk`` cluster the warmup Jobs survive `az aks stop`/`start`
    (stable node names) so ``k8s_ensure_job_manifests`` would skip recreating
    them and the RAM cache would stay cold. ``force_rewarm=True`` must call
    ``k8s_release_warmup_cache`` for the DB before ensure so fresh Jobs run.

    The job-ensure step is stubbed to fail so the task stops deterministically
    right after the release/ensure pair — the assertion is about the *ordering*
    (release happened, then ensure ran), not the full warmup completion.
    """

    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    monkeypatch.setenv("ELB_LOCAL_STATE_DIR", str(tmp_path))
    monkeypatch.setattr("api.tasks.storage.get_credential", lambda: object())
    monkeypatch.setattr(
        "api.services.storage.data.list_databases",
        lambda credential, storage_account: [
            {
                "name": "core_nt",
                "file_count": 12,
                "sharded": True,
                "shard_sets": [4],
                "source_version": "2026-05-20-00-00-00",
            }
        ],
    )
    _patch_ready_warmup_nodes(monkeypatch, 4)

    class _Plan:
        nodes = ("aks-blast-000000",)
        jobs = ({"metadata": {"name": "warm-core-nt-00", "namespace": "default"}},)

    monkeypatch.setattr(
        "api.services.warmup.jobs.build_warmup_job_plan",
        lambda **kwargs: _Plan(),
    )
    monkeypatch.setattr(
        "api.services.k8s.monitoring.k8s_ensure_warmup_scripts_configmap",
        lambda *a, **k: {"status": "unchanged"},
    )

    order: list[str] = []
    release_calls: list[tuple[Any, Any]] = []

    def _record_release(cred, sub, rg, cluster, db_name, *a, **k):
        order.append("release")
        release_calls.append((cluster, db_name))
        return {"status": "released", "database": db_name}

    monkeypatch.setattr(
        "api.services.k8s.monitoring.k8s_release_warmup_cache", _record_release
    )
    monkeypatch.setattr(
        "api.services.k8s.monitoring.k8s_release_stale_warmup_jobs",
        lambda *a, **k: {"status": "released", "deleted": []},
    )

    def _ensure(*a, **k):
        order.append("ensure")
        return {"error_count": 1, "errors": ["stop-here"], "created_count": 0}

    monkeypatch.setattr("api.services.k8s.monitoring.k8s_ensure_job_manifests", _ensure)
    monkeypatch.setattr("api.tasks.storage._update_state", lambda *a, **k: None)
    monkeypatch.setattr("api.tasks.storage._record_task_progress", lambda *a, **k: None)

    result = warmup_database.run(
        job_id="auto-warmup-elb-cluster-core_nt-1",
        subscription_id="sub-1",
        resource_group="rg-elb",
        storage_account="elbstg01",
        database_name="core_nt",
        storage_resource_group="rg-elb",
        cluster_name="elb-cluster",
        machine_type="Standard_E16s_v5",
        num_nodes=1,
        acr_name="elbacr01",
        acr_resource_group="rg-acr",
        require_all_warmup_nodes=True,
        force_rewarm=True,
    )

    # The forced re-warm released the DB's existing Jobs and that release ran
    # BEFORE ensure recreated them.
    assert release_calls == [("elb-cluster", "core_nt")]
    assert order == ["release", "ensure"]
    # ensure was stubbed to fail, so the task surfaces that as a failure.
    assert result["status"] == "failed"


def test_warmup_database_force_rewarm_defaults_off(monkeypatch, tmp_path) -> None:
    """Without force_rewarm the task must NOT blanket-release the DB Jobs."""

    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    monkeypatch.setenv("ELB_LOCAL_STATE_DIR", str(tmp_path))
    monkeypatch.setattr("api.tasks.storage.get_credential", lambda: object())
    monkeypatch.setattr(
        "api.services.storage.data.list_databases",
        lambda credential, storage_account: [
            {
                "name": "core_nt",
                "file_count": 12,
                "sharded": True,
                "shard_sets": [4],
                "source_version": "2026-05-20-00-00-00",
            }
        ],
    )
    _patch_ready_warmup_nodes(monkeypatch, 4)

    class _Plan:
        nodes = ("aks-blast-000000",)
        jobs = ({"metadata": {"name": "warm-core-nt-00", "namespace": "default"}},)

    monkeypatch.setattr(
        "api.services.warmup.jobs.build_warmup_job_plan",
        lambda **kwargs: _Plan(),
    )
    monkeypatch.setattr(
        "api.services.k8s.monitoring.k8s_ensure_warmup_scripts_configmap",
        lambda *a, **k: {"status": "unchanged"},
    )

    release_calls: list[Any] = []
    monkeypatch.setattr(
        "api.services.k8s.monitoring.k8s_release_warmup_cache",
        lambda *a, **k: release_calls.append(a) or {"status": "released"},
    )
    monkeypatch.setattr(
        "api.services.k8s.monitoring.k8s_release_stale_warmup_jobs",
        lambda *a, **k: {"status": "released", "deleted": []},
    )
    monkeypatch.setattr(
        "api.services.k8s.monitoring.k8s_ensure_job_manifests",
        lambda *a, **k: {"error_count": 1, "errors": ["stop-here"], "created_count": 0},
    )
    monkeypatch.setattr("api.tasks.storage._update_state", lambda *a, **k: None)
    monkeypatch.setattr("api.tasks.storage._record_task_progress", lambda *a, **k: None)

    warmup_database.run(
        job_id="auto-warmup-elb-cluster-core_nt-1",
        subscription_id="sub-1",
        resource_group="rg-elb",
        storage_account="elbstg01",
        database_name="core_nt",
        storage_resource_group="rg-elb",
        cluster_name="elb-cluster",
        machine_type="Standard_E16s_v5",
        num_nodes=1,
        acr_name="elbacr01",
        acr_resource_group="rg-acr",
        require_all_warmup_nodes=True,
    )

    assert release_calls == [], "k8s_release_warmup_cache must not run without force_rewarm"


def test_warmup_database_force_rewarm_partial_release_fails_loudly(
    monkeypatch, tmp_path
) -> None:
    """A partial forced release must abort before ensure (no silent subset warm).

    If `k8s_release_warmup_cache` returns ``status="partial"`` a stale Job
    survived; its name still exists so `k8s_ensure_job_manifests` would skip
    recreating it, leaving that shard cold. The task must raise instead of
    reporting success so Celery autoretry re-runs the release.
    """

    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    monkeypatch.setenv("ELB_LOCAL_STATE_DIR", str(tmp_path))
    monkeypatch.setattr("api.tasks.storage.get_credential", lambda: object())
    monkeypatch.setattr(
        "api.services.storage.data.list_databases",
        lambda credential, storage_account: [
            {
                "name": "core_nt",
                "file_count": 12,
                "sharded": True,
                "shard_sets": [4],
                "source_version": "2026-05-20-00-00-00",
            }
        ],
    )
    _patch_ready_warmup_nodes(monkeypatch, 4)

    class _Plan:
        nodes = ("aks-blast-000000",)
        jobs = ({"metadata": {"name": "warm-core-nt-00", "namespace": "default"}},)

    monkeypatch.setattr(
        "api.services.warmup.jobs.build_warmup_job_plan",
        lambda **kwargs: _Plan(),
    )
    monkeypatch.setattr(
        "api.services.k8s.monitoring.k8s_ensure_warmup_scripts_configmap",
        lambda *a, **k: {"status": "unchanged"},
    )
    monkeypatch.setattr(
        "api.services.k8s.monitoring.k8s_release_warmup_cache",
        lambda *a, **k: {"status": "partial", "errors": [{"kind": "jobs", "status_code": 500}]},
    )
    monkeypatch.setattr(
        "api.services.k8s.monitoring.k8s_release_stale_warmup_jobs",
        lambda *a, **k: {"status": "released", "deleted": []},
    )
    ensure_calls: list[Any] = []
    monkeypatch.setattr(
        "api.services.k8s.monitoring.k8s_ensure_job_manifests",
        lambda *a, **k: ensure_calls.append(a) or {"created_count": 1, "error_count": 0},
    )
    monkeypatch.setattr("api.tasks.storage._update_state", lambda *a, **k: None)
    monkeypatch.setattr("api.tasks.storage._record_task_progress", lambda *a, **k: None)

    result = warmup_database.run(
        job_id="auto-warmup-elb-cluster-core_nt-1",
        subscription_id="sub-1",
        resource_group="rg-elb",
        storage_account="elbstg01",
        database_name="core_nt",
        storage_resource_group="rg-elb",
        cluster_name="elb-cluster",
        machine_type="Standard_E16s_v5",
        num_nodes=1,
        acr_name="elbacr01",
        acr_resource_group="rg-acr",
        require_all_warmup_nodes=True,
        force_rewarm=True,
    )

    # A partial release must abort the warmup: ensure must NOT run and the
    # task must surface failure (so autoretry re-runs the release).
    assert ensure_calls == [], "ensure must not run after a partial forced release"
    assert result["status"] == "failed"


def test_warmup_database_fails_fast_when_storage_resource_group_missing(
    monkeypatch,
    tmp_path,
) -> None:
    """warmup_database must refuse to run without a Storage RG.

    Regression guard for the silent ARM-404 path: when the caller forgets to
    forward `storage_resource_group`, the task previously fell back to the
    AKS cluster RG, looked the Storage account up there, got a benign
    ResourceNotFound, swallowed the exception and proceeded with no RBAC
    ensure. The downstream K8s warmup Job then failed per-node because the
    AKS kubelet identity could not read the storage container.
    """
    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    monkeypatch.setenv("ELB_LOCAL_STATE_DIR", str(tmp_path))
    monkeypatch.setattr("api.tasks.storage.get_credential", lambda: object())

    state_updates: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "api.tasks.storage._update_state",
        lambda job_id, phase, status="running", **extra: state_updates.append(
            {"job_id": job_id, "phase": phase, "status": status, **extra}
        ),
    )
    monkeypatch.setattr(
        "api.tasks.storage._record_task_progress",
        lambda task, phase, **meta: None,
    )

    # If validation is bypassed, this would be reached and raise — the test
    # would still pass via the exception, but the assertion below pins the
    # contract that validation runs *before* any storage access.
    def _should_not_be_called(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("list_databases must not be called when validation fails")

    monkeypatch.setattr("api.services.storage.data.list_databases", _should_not_be_called)

    result = warmup_database.run(
        job_id="auto-warmup-elb-cluster-core_nt-1",
        subscription_id="sub-1",
        resource_group="rg-elb-cluster",
        storage_account="stelbdashboardxyz",
        database_name="core_nt",
        # storage_resource_group intentionally omitted ↓ — defaults to "".
        cluster_name="elb-cluster",
        machine_type="Standard_E16s_v5",
        num_nodes=4,
        acr_name="elbacr01",
        require_all_warmup_nodes=True,
    )

    assert result["status"] == "failed"
    assert "storage_resource_group" in result["error"]
    assert any(
        update["status"] == "failed" and "storage_resource_group" in update.get("error_code", "")
        for update in state_updates
    )


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


def test_auto_warmup_table_store_uses_dedicated_table(monkeypatch) -> None:
    created_tables: list[str] = []
    writes: list[dict[str, object]] = []

    class RecordingTableClient:
        def __init__(self, **kwargs: object) -> None:
            self.table_name = str(kwargs["table_name"])

        def __enter__(self) -> RecordingTableClient:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def upsert_entity(self, entity: dict[str, object], *, mode: object) -> None:
            writes.append({"table_name": self.table_name, "mode": mode, **entity})

    class RecordingTableService:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> RecordingTableService:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def create_table_if_not_exists(self, table_name: str) -> None:
            created_tables.append(table_name)

    monkeypatch.setenv("AZURE_TABLE_ENDPOINT", "https://acct.table.core.windows.net")
    monkeypatch.setenv("CONTAINER_APP_NAME", "ca-elb-dashboard")
    monkeypatch.setattr(auto_warmup, "TableClient", RecordingTableClient)
    monkeypatch.setattr(auto_warmup, "TableServiceClient", RecordingTableService)
    monkeypatch.setattr(auto_warmup, "get_credential", lambda: object())
    auto_warmup._ENSURED_TABLES.clear()

    save_auto_warmup_preference(
        AutoWarmupPreference(
            subscription_id="sub-1",
            resource_group="rg-elb",
            cluster_name="elb-cluster",
            storage_account="elbstg01",
            storage_resource_group="rg-elb",
        )
    )

    assert created_tables == ["autowarmup"]
    assert writes[0]["table_name"] == "autowarmup"
    assert writes[0]["PartitionKey"] == "auto_warmup:sub-1:rg-elb:elb-cluster"
    assert writes[0]["type"] == "auto_warmup"


def test_auto_warmup_file_backend_save_does_not_create_lock_sentinel(
    monkeypatch, tmp_path
) -> None:
    """Critique #14 (auto_warmup mirror): the file backend used to leave
    an orphan ``auto_warmup.json.lock`` sentinel after every save. The
    fix replaces the sibling-file ``fcntl.flock`` with an in-process
    ``threading.Lock`` keyed by the state file path, so no ``.lock``
    file is created at all.
    """
    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    monkeypatch.setenv("ELB_LOCAL_STATE_DIR", str(tmp_path))

    save_auto_warmup_preference(
        AutoWarmupPreference(
            subscription_id="sub-1",
            resource_group="rg-elb",
            cluster_name="elb-cluster",
            storage_account="elbstg01",
            storage_resource_group="rg-elb",
        )
    )
    save_auto_warmup_preference(
        AutoWarmupPreference(
            subscription_id="sub-1",
            resource_group="rg-elb",
            cluster_name="elb-cluster-2",
            storage_account="elbstg01",
            storage_resource_group="rg-elb",
        )
    )

    files = {p.name for p in tmp_path.iterdir()}
    assert "auto_warmup.json" in files
    assert not any(name.endswith(".lock") for name in files), files
