"""Tests for Azure Provision AKS behavior.

Responsibility: Tests for Azure Provision AKS behavior
Edit boundaries: Keep assertions focused on the behavior under test; prefer fakes over live
Azure calls.
Key entry points: `test_build_cluster_params_enables_blob_csi_driver`,
`test_build_cluster_params_keeps_expected_pools_and_taints`,
`test_provision_aks_ensures_resource_group_before_create`
Risky contracts: Do not require network access or real Azure credentials unless the test is
explicitly integration-scoped.
Validation: `uv run pytest -q api/tests/test_azure_provision_aks.py`.
"""

from __future__ import annotations

from typing import Any

import pytest
from api.tasks.azure import _build_cluster_params


def test_build_cluster_params_enables_blob_csi_driver() -> None:
    cluster = _build_cluster_params(
        region="koreacentral",
        cluster_name="elb-smoke-aks",
        sys_sku="Standard_D2s_v3",
        sys_count=1,
        blast_sku="Standard_D8s_v3",
        blast_count=1,
        caller_oid="caller-1",
    )

    assert cluster.storage_profile.blob_csi_driver.enabled is True


def test_build_cluster_params_keeps_expected_pools_and_taints() -> None:
    cluster = _build_cluster_params(
        region="koreacentral",
        cluster_name="elb-smoke-aks",
        sys_sku="Standard_D2s_v3",
        sys_count=1,
        blast_sku="Standard_D8s_v3",
        blast_count=2,
        caller_oid="caller-1",
    )

    pools = {pool.name: pool for pool in cluster.agent_pool_profiles}
    assert pools["systempool"].node_taints == ["CriticalAddonsOnly=true:NoSchedule"]
    assert pools["blastpool"].node_labels == {"workload": "blast"}
    assert pools["blastpool"].node_taints == ["workload=blast:NoSchedule"]


def test_provision_aks_ensures_resource_group_before_create(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """provision_aks must create a missing resource group before AKS create.

    The SPA defaults the RG to `rg-<base-name>` which may not exist on a fresh
    subscription; without this idempotent ensure the AKS create would fail ~10
    min in with ResourceGroupNotFound.
    """
    import api.tasks.azure as azure
    from api.tasks.azure import provision_aks
    from azure.core.exceptions import ResourceNotFoundError

    call_log: list[str] = []

    class FakeResourceGroups:
        def create_or_update(self, rg_name: str, body: dict[str, Any]) -> object:
            call_log.append(f"rg.create_or_update:{rg_name}:{body.get('location', '')}")
            return object()

        def get(self, rg_name: str) -> object:
            call_log.append(f"rg.get:{rg_name}")
            if call_log == [f"rg.get:{rg_name}"]:
                raise ResourceNotFoundError(message="missing")
            return object()

    class FakeRc:
        resource_groups = FakeResourceGroups()

    class FakePoller:
        def result(self) -> object:
            class _Cluster:
                identity = type("I", (), {"principal_id": "mi-principal"})()
                provisioning_state = "Succeeded"
                node_resource_group = "MC_rg-test_elb-cluster_koreacentral"

            return _Cluster()

    class FakeManagedClusters:
        def begin_create_or_update(
            self, rg: str, name: str, params: object
        ) -> FakePoller:
            call_log.append(f"aks.begin_create_or_update:{rg}:{name}")
            return FakePoller()

    class FakeAksClient:
        managed_clusters = FakeManagedClusters()

    monkeypatch.setattr(azure, "get_credential", lambda: object())
    monkeypatch.setattr(azure, "aks_client", lambda _cred, _sub: FakeAksClient())
    monkeypatch.setattr(azure, "resource_client", lambda _cred, _sub: FakeRc())
    # Skip the runtime-RBAC stage — not under test here.
    monkeypatch.setattr(
        azure,
        "_ensure_aks_runtime_rbac",
        lambda *_args, **_kwargs: {
            "acr_attached": False,
            "storage_role_granted": False,
            "roles_assigned": [],
            "roles_failed": [],
        },
    )

    provision_aks.run(
        job_id="job-1",
        subscription_id="sub-1",
        resource_group="rg-elb-cluster",
        region="koreacentral",
        cluster_name="elb-cluster-01",
        node_sku="Standard_D8s_v3",
        node_count=1,
        system_vm_size="Standard_D2s_v3",
        system_node_count=1,
        acr_resource_group="",
        acr_name="",
        storage_resource_group="",
        storage_account="",
        caller_oid="caller-1",
    )

    # The task probes first, then creates the missing RG with the requested region.
    assert call_log[0] == "rg.get:rg-elb-cluster", call_log
    assert call_log[1] == "rg.create_or_update:rg-elb-cluster:koreacentral", call_log
    # Then the eventual-consistency visibility check runs against the created RG.
    assert call_log[2] == "rg.get:rg-elb-cluster", call_log
    # Then the AKS create against the same RG.
    assert call_log[3] == "aks.begin_create_or_update:rg-elb-cluster:elb-cluster-01", call_log


def test_provision_aks_does_not_recreate_existing_rg_with_different_region(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Existing RG locations are immutable, so cross-region AKS creates must
    not re-submit the resource group with the AKS region.
    """
    import api.tasks.azure as azure
    from api.tasks.azure import provision_aks

    call_log: list[str] = []

    class FakeResourceGroups:
        def create_or_update(self, rg_name: str, body: dict[str, Any]) -> object:
            call_log.append(f"rg.create_or_update:{rg_name}:{body.get('location', '')}")
            return object()

        def get(self, rg_name: str) -> object:
            call_log.append(f"rg.get:{rg_name}")
            return object()

    class FakeRc:
        resource_groups = FakeResourceGroups()

    class FakePoller:
        def result(self) -> object:
            class _Cluster:
                identity = type("I", (), {"principal_id": "mi-principal"})()
                provisioning_state = "Succeeded"
                node_resource_group = "MC_rg-test_elb-cluster_eastus2"

            return _Cluster()

    class FakeManagedClusters:
        def begin_create_or_update(
            self, rg: str, name: str, params: object
        ) -> FakePoller:
            call_log.append(f"aks.begin_create_or_update:{rg}:{name}")
            return FakePoller()

    class FakeAksClient:
        managed_clusters = FakeManagedClusters()

    monkeypatch.setattr(azure, "get_credential", lambda: object())
    monkeypatch.setattr(azure, "aks_client", lambda _cred, _sub: FakeAksClient())
    monkeypatch.setattr(azure, "resource_client", lambda _cred, _sub: FakeRc())
    monkeypatch.setattr(
        azure,
        "_ensure_aks_runtime_rbac",
        lambda *_args, **_kwargs: {
            "acr_attached": False,
            "storage_role_granted": False,
            "roles_assigned": [],
            "roles_failed": [],
        },
    )

    provision_aks.run(
        job_id="job-1",
        subscription_id="sub-1",
        resource_group="rg-elb-cluster",
        region="eastus2",
        cluster_name="elb-cluster-01",
        node_sku="Standard_D8s_v3",
        node_count=1,
        system_vm_size="Standard_D2s_v3",
        system_node_count=1,
        acr_resource_group="",
        acr_name="",
        storage_resource_group="",
        storage_account="",
        caller_oid="caller-1",
    )

    assert "rg.create_or_update:rg-elb-cluster:eastus2" not in call_log
    assert call_log[0] == "rg.get:rg-elb-cluster", call_log
    assert call_log[1] == "rg.get:rg-elb-cluster", call_log
    assert call_log[2] == "aks.begin_create_or_update:rg-elb-cluster:elb-cluster-01", call_log


def test_provision_aks_retries_when_rg_not_yet_visible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If ARM has not yet propagated the freshly-created RG, the visibility
    poll must retry until `get` succeeds, then proceed to the AKS create.
    Without this guard, AKS occasionally returns ResourceGroupNotFound for a
    RG that was just created in the same task.
    """
    import api.tasks.azure as azure
    from api.tasks.azure import provision as provision_mod
    from api.tasks.azure import provision_aks
    from azure.core.exceptions import ResourceNotFoundError

    call_log: list[str] = []
    get_attempts = {"n": 0}

    class FakeResourceGroups:
        def create_or_update(self, rg_name: str, body: dict[str, Any]) -> object:
            call_log.append(f"rg.create_or_update:{rg_name}")
            return object()

        def get(self, rg_name: str) -> object:
            get_attempts["n"] += 1
            call_log.append(f"rg.get:{rg_name}:{get_attempts['n']}")
            if get_attempts["n"] < 3:
                raise ResourceNotFoundError(message="not yet visible")
            return object()

    class FakeRc:
        resource_groups = FakeResourceGroups()

    class FakePoller:
        def result(self) -> object:
            class _Cluster:
                identity = type("I", (), {"principal_id": "mi-principal"})()
                provisioning_state = "Succeeded"
                node_resource_group = "MC_rg-test_elb-cluster_koreacentral"

            return _Cluster()

    class FakeManagedClusters:
        def begin_create_or_update(
            self, rg: str, name: str, params: object
        ) -> FakePoller:
            call_log.append(f"aks.begin_create_or_update:{rg}:{name}")
            return FakePoller()

    class FakeAksClient:
        managed_clusters = FakeManagedClusters()

    sleep_calls: list[float] = []
    monkeypatch.setattr(provision_mod.time, "sleep", lambda secs: sleep_calls.append(secs))
    monkeypatch.setattr(azure, "get_credential", lambda: object())
    monkeypatch.setattr(azure, "aks_client", lambda _cred, _sub: FakeAksClient())
    monkeypatch.setattr(azure, "resource_client", lambda _cred, _sub: FakeRc())
    monkeypatch.setattr(
        azure,
        "_ensure_aks_runtime_rbac",
        lambda *_args, **_kwargs: {
            "acr_attached": False,
            "storage_role_granted": False,
            "roles_assigned": [],
            "roles_failed": [],
        },
    )

    provision_aks.run(
        job_id="job-1",
        subscription_id="sub-1",
        resource_group="rg-elb-cluster",
        region="koreacentral",
        cluster_name="elb-cluster-01",
        node_sku="Standard_D8s_v3",
        node_count=1,
        system_vm_size="Standard_D2s_v3",
        system_node_count=1,
        acr_resource_group="",
        acr_name="",
        storage_resource_group="",
        storage_account="",
        caller_oid="caller-1",
    )

    # The task must have called `get` until success and slept between attempts.
    assert get_attempts["n"] == 3, call_log
    # The first failed get triggers create_or_update immediately; only the
    # post-create visibility poll sleeps between failed attempts.
    assert len(sleep_calls) == 1, sleep_calls
    # The AKS create only runs after the RG becomes visible.
    aks_idx = next(i for i, line in enumerate(call_log) if line.startswith("aks."))
    rg_get_indices = [i for i, line in enumerate(call_log) if line.startswith("rg.get:")]
    assert rg_get_indices[-1] < aks_idx, call_log


def test_provision_aks_publishes_step_progress_with_pool_states(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """provision_aks must publish per-phase progress to Celery `result.info`.

    The provisioning banner reads `progress` from `/api/tasks/{id}` (=Celery
    `result.info`). Without these `task.update_state(state="PROGRESS",
    meta=…)` calls the user sees a blank "Provisioning…" timer for 5-10
    minutes. The new `_publish` helper must always include `phase`, `step`,
    and `total_steps`, and the ARM-poll loop must publish a `pools` snapshot
    plus a `cluster_state` every tick once the cluster becomes visible.
    """
    import api.tasks.azure as azure
    from api.tasks.azure import provision as provision_mod
    from api.tasks.azure import provision_aks
    from azure.core.exceptions import ResourceNotFoundError

    class FakeResourceGroups:
        def create_or_update(self, *_args: Any, **_kwargs: Any) -> object:
            return object()

        def get(self, _rg: str) -> object:
            return object()

    class FakeRc:
        resource_groups = FakeResourceGroups()

    # Poller exposes `done()` so the new sub-progress loop runs at least once.
    poll_ticks = {"n": 0}

    class FakePoller:
        def done(self) -> bool:
            poll_ticks["n"] += 1
            # Force exactly one tick of the sub-progress loop before the
            # task moves on to result().
            return poll_ticks["n"] > 1

        def result(self) -> object:
            class _Cluster:
                identity = type("I", (), {"principal_id": "mi-principal"})()
                provisioning_state = "Succeeded"
                node_resource_group = "MC_rg_x_koreacentral"

            return _Cluster()

    class FakeAgentPool:
        def __init__(self, name: str, state: str, count: int, vm: str, mode: str) -> None:
            self.name = name
            self.provisioning_state = state
            self.count = count
            self.vm_size = vm
            self.mode = mode

    class FakeAgentPools:
        def list(self, _rg: str, _cluster: str) -> list[FakeAgentPool]:
            return [
                FakeAgentPool("systempool", "Succeeded", 1, "Standard_D2s_v3", "System"),
                FakeAgentPool("blastpool", "Creating", 2, "Standard_D8s_v3", "User"),
            ]

    class FakeManagedClusters:
        def begin_create_or_update(self, _rg: str, _name: str, _params: object) -> FakePoller:
            return FakePoller()

        def get(self, _rg: str, _name: str) -> object:
            class _C:
                provisioning_state = "Creating"

            return _C()

    class FakeAksClient:
        managed_clusters = FakeManagedClusters()
        agent_pools = FakeAgentPools()

    # Capture every Celery progress publish so we can assert the new shape.
    publishes: list[dict[str, Any]] = []

    def _capture(state: str, meta: dict[str, Any] | None = None, **_kw: Any) -> None:
        publishes.append({"state": state, "meta": dict(meta or {})})

    # `provision_aks` is a Celery Task instance; replacing its `update_state`
    # attribute makes every `self.update_state(state=…, meta=…)` call inside
    # the task body land in our capture list. The plain-function form means
    # Python does not implicitly bind `self` — exactly what we want here.
    monkeypatch.setattr(provision_aks, "update_state", _capture)
    # Skip the real sleeps inside the ARM poll loop and the RG visibility loop.
    monkeypatch.setattr(provision_mod.time, "sleep", lambda _secs: None)
    monkeypatch.setattr(azure, "get_credential", lambda: object())
    monkeypatch.setattr(azure, "aks_client", lambda _cred, _sub: FakeAksClient())
    monkeypatch.setattr(azure, "resource_client", lambda _cred, _sub: FakeRc())
    monkeypatch.setattr(
        azure,
        "_ensure_aks_runtime_rbac",
        lambda *_args, **_kwargs: {
            "acr_attached": True,
            "storage_role_granted": True,
            "roles_assigned": ["AcrPull", "Storage Blob Data Contributor"],
            "roles_failed": [],
        },
    )

    provision_aks.run(
        job_id="job-progress",
        subscription_id="sub-1",
        resource_group="rg-elb-cluster",
        region="koreacentral",
        cluster_name="elb-cluster-01",
        node_sku="Standard_D8s_v3",
        node_count=2,
        system_vm_size="Standard_D2s_v3",
        system_node_count=1,
        acr_resource_group="",
        acr_name="",
        storage_resource_group="",
        storage_account="",
        caller_oid="caller-1",
    )

    # Defensive: at least four publishes (creating → ensuring_rg → arm → ensuring_rbac → completed).
    phases = [p["meta"].get("phase") for p in publishes if p["state"] == "PROGRESS"]
    assert "creating_cluster" in phases, phases
    assert "ensuring_resource_group" in phases, phases
    assert "arm_create_or_update" in phases, phases
    assert "ensuring_rbac" in phases, phases
    assert "completed" in phases, phases

    # Every progress publish must carry step + total_steps so the banner
    # can render "Step N/M".
    for p in publishes:
        if p["state"] != "PROGRESS":
            continue
        meta = p["meta"]
        assert isinstance(meta.get("step"), int), meta
        assert isinstance(meta.get("total_steps"), int), meta
        assert meta["total_steps"] >= meta["step"]

    # The ARM sub-progress tick must include a `pools` snapshot and a
    # `cluster_state` once the cluster is visible. Find the loop tick
    # (not the initial Submitting publish) by `cluster_state` presence.
    arm_ticks = [
        p["meta"] for p in publishes
        if p["meta"].get("phase") == "arm_create_or_update"
        and "cluster_state" in p["meta"]
    ]
    all_arm = [
        p["meta"]
        for p in publishes
        if p["meta"].get("phase") == "arm_create_or_update"
    ]
    assert arm_ticks, all_arm
    tick = arm_ticks[0]
    assert tick["cluster_state"] == "Creating"
    pools = tick.get("pools") or []
    pool_names = sorted(pp.get("name") for pp in pools)
    assert pool_names == ["blastpool", "systempool"], pools
    by_name = {pp["name"]: pp for pp in pools}
    assert by_name["systempool"]["state"] == "Succeeded"
    assert by_name["blastpool"]["state"] == "Creating"

    # ResourceNotFoundError-on-list path is exercised by other tests; just
    # confirm we did not crash here when both pools were visible.
    _ = ResourceNotFoundError  # silence import-unused
