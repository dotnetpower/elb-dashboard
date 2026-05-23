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
    """provision_aks must call resource_groups.create_or_update BEFORE the
    AKS create. The SPA defaults the RG to `rg-<base-name>` which may not
    exist on a fresh subscription; without this idempotent ensure the AKS
    create would fail ~10 min in with ResourceGroupNotFound.
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

    # The first Azure call must be the RG ensure, with the requested region.
    assert call_log[0] == "rg.create_or_update:rg-elb-cluster:koreacentral", call_log
    # Then the eventual-consistency visibility check.
    assert call_log[1] == "rg.get:rg-elb-cluster", call_log
    # Then the AKS create against the same RG.
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
    assert len(sleep_calls) == 2, sleep_calls
    # The AKS create only runs after the RG becomes visible.
    aks_idx = next(i for i, line in enumerate(call_log) if line.startswith("aks."))
    rg_get_indices = [i for i, line in enumerate(call_log) if line.startswith("rg.get:")]
    assert rg_get_indices[-1] < aks_idx, call_log
