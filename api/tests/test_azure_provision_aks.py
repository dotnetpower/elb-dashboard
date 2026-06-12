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


def test_build_cluster_params_enables_oidc_and_workload_identity() -> None:
    """Regression guard for the 2026-05-26 'wi=null' incident.

    The AKS API accepts a cluster with `oidcIssuerProfile.enabled=True`
    while `securityProfile.workloadIdentity` is unset (the field is
    nullable on the server side). The dashboard's OpenAPI deploy then
    succeeds at creating the federated identity credential but the
    OpenAPI pod hangs on the token swap because the workload-identity
    mutating webhook is not installed in the cluster. Pin both flags so
    `az aks update --enable-workload-identity` is never required as a
    post-create follow-up.
    """
    cluster = _build_cluster_params(
        region="koreacentral",
        cluster_name="elb-smoke-aks",
        sys_sku="Standard_D2s_v3",
        sys_count=1,
        blast_sku="Standard_D8s_v3",
        blast_count=1,
        caller_oid="caller-1",
    )

    assert cluster.oidc_issuer_profile is not None
    assert cluster.oidc_issuer_profile.enabled is True
    assert cluster.security_profile is not None
    assert cluster.security_profile.workload_identity is not None
    assert cluster.security_profile.workload_identity.enabled is True


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


def test_build_cluster_params_managed_vnet_when_no_subnet() -> None:
    """Without a subnet id the cluster stays in managed-VNet mode.

    Backward-compat guard: local/legacy callers that do not inject a subnet
    id must keep producing a model with no per-pool vnet_subnet_id and no
    explicit network profile (Azure picks the node subnet).
    """
    cluster = _build_cluster_params(
        region="koreacentral",
        cluster_name="elb-smoke-aks",
        sys_sku="Standard_D2s_v3",
        sys_count=1,
        blast_sku="Standard_D8s_v3",
        blast_count=1,
        caller_oid="caller-1",
    )

    assert cluster.network_profile is None
    for pool in cluster.agent_pool_profiles:
        assert pool.vnet_subnet_id is None


def test_build_cluster_params_byo_subnet_pins_overlay() -> None:
    """A supplied subnet id puts both pools in BYO-subnet mode + overlay.

    This is the storage-connectivity fix: nodes land in the hub snet-aks
    subnet so the workload Storage private endpoints resolve and route
    intra-VNet, and Azure CNI Overlay keeps pods off the subnet IP space.
    """
    subnet_id = (
        "/subscriptions/sub/resourceGroups/rg-elb-dashboard/providers/"
        "Microsoft.Network/virtualNetworks/vnet-elb-dashboard/subnets/snet-aks"
    )
    cluster = _build_cluster_params(
        region="koreacentral",
        cluster_name="elb-smoke-aks",
        sys_sku="Standard_D2s_v3",
        sys_count=1,
        blast_sku="Standard_D8s_v3",
        blast_count=1,
        caller_oid="caller-1",
        vnet_subnet_id=subnet_id,
    )

    for pool in cluster.agent_pool_profiles:
        assert pool.vnet_subnet_id == subnet_id
    assert cluster.network_profile is not None
    assert cluster.network_profile.network_plugin == "azure"
    assert cluster.network_profile.network_plugin_mode == "overlay"
    assert cluster.network_profile.pod_cidr == "10.244.0.0/16"


def test_build_cluster_params_default_warm_cache_is_byte_identical() -> None:
    """The default `ephemeral` mode must not change the blastpool OS disk.

    Regression guard: a missing Performance preference reads back as
    `ephemeral`, and that path must leave the blastpool disk fields unset so
    the ARM payload stays byte-identical to the historical default (Azure
    picks an ephemeral OS disk when the SKU cache allows).
    """
    default = _build_cluster_params(
        region="koreacentral",
        cluster_name="elb-smoke-aks",
        sys_sku="Standard_D2s_v3",
        sys_count=1,
        blast_sku="Standard_D8s_v3",
        blast_count=1,
        caller_oid="caller-1",
    )
    explicit = _build_cluster_params(
        region="koreacentral",
        cluster_name="elb-smoke-aks",
        sys_sku="Standard_D2s_v3",
        sys_count=1,
        blast_sku="Standard_D8s_v3",
        blast_count=1,
        caller_oid="caller-1",
        warm_cache_mode="ephemeral",
    )
    for cluster in (default, explicit):
        pools = {pool.name: pool for pool in cluster.agent_pool_profiles}
        assert pools["blastpool"].os_disk_type is None
        assert pools["blastpool"].os_disk_size_gb is None
        assert "elb-warm-cache" not in (cluster.tags or {})


def test_build_cluster_params_node_disk_pins_managed_os_disk() -> None:
    cluster = _build_cluster_params(
        region="koreacentral",
        cluster_name="elb-smoke-aks",
        sys_sku="Standard_D2s_v3",
        sys_count=1,
        blast_sku="Standard_D8s_v3",
        blast_count=1,
        caller_oid="caller-1",
        warm_cache_mode="node_disk",
    )
    pools = {pool.name: pool for pool in cluster.agent_pool_profiles}
    assert pools["blastpool"].os_disk_type == "Managed"
    assert pools["blastpool"].os_disk_size_gb == 512
    # The systempool must stay untouched.
    assert pools["systempool"].os_disk_type is None
    assert pools["systempool"].os_disk_size_gb is None
    assert (cluster.tags or {}).get("elb-warm-cache") == "node_disk"


def test_build_cluster_params_data_disk_tags_but_keeps_default_disk() -> None:
    """`data_disk` is realised by a PVC in the warmup task, not the cluster
    model, so the blastpool disk fields stay default — only the tag is set."""
    cluster = _build_cluster_params(
        region="koreacentral",
        cluster_name="elb-smoke-aks",
        sys_sku="Standard_D2s_v3",
        sys_count=1,
        blast_sku="Standard_D8s_v3",
        blast_count=1,
        caller_oid="caller-1",
        warm_cache_mode="data_disk",
    )
    pools = {pool.name: pool for pool in cluster.agent_pool_profiles}
    assert pools["blastpool"].os_disk_type is None
    assert pools["blastpool"].os_disk_size_gb is None
    assert (cluster.tags or {}).get("elb-warm-cache") == "data_disk"


def test_resolve_aks_vnet_subnet_id_prefers_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api.tasks.azure.provision import _resolve_aks_vnet_subnet_id

    monkeypatch.setenv("PLATFORM_AKS_SUBNET_ID", "/explicit/snet-aks")
    monkeypatch.setenv(
        "PLATFORM_PRIVATE_ENDPOINT_SUBNET_ID",
        "/vnet/subnets/snet-private-endpoints",
    )
    assert _resolve_aks_vnet_subnet_id() == "/explicit/snet-aks"


def test_resolve_aks_vnet_subnet_id_derives_from_pe_subnet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api.tasks.azure.provision import _resolve_aks_vnet_subnet_id

    monkeypatch.delenv("PLATFORM_AKS_SUBNET_ID", raising=False)
    monkeypatch.setenv(
        "PLATFORM_PRIVATE_ENDPOINT_SUBNET_ID",
        "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Network/"
        "virtualNetworks/vnet-elb-dashboard/subnets/snet-private-endpoints",
    )
    assert _resolve_aks_vnet_subnet_id() == (
        "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Network/"
        "virtualNetworks/vnet-elb-dashboard/subnets/snet-aks"
    )


def test_resolve_aks_vnet_subnet_id_empty_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api.tasks.azure.provision import _resolve_aks_vnet_subnet_id

    monkeypatch.delenv("PLATFORM_AKS_SUBNET_ID", raising=False)
    monkeypatch.delenv("PLATFORM_PRIVATE_ENDPOINT_SUBNET_ID", raising=False)
    assert _resolve_aks_vnet_subnet_id() == ""


def test_grant_network_contributor_on_subnet_creates_assignment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import api.tasks.azure.rbac as rbac

    created: list[dict[str, object]] = []

    class _FakeRoleAssignments:
        def create(
            self, scope: str, role_assignment_name: str, parameters: object
        ) -> object:
            created.append(
                {
                    "scope": scope,
                    "name": role_assignment_name,
                    "role_definition_id": parameters.role_definition_id,
                    "principal_id": parameters.principal_id,
                }
            )
            return object()

    class _FakeAuthClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.role_assignments = _FakeRoleAssignments()

    monkeypatch.setattr(
        "azure.mgmt.authorization.AuthorizationManagementClient",
        _FakeAuthClient,
    )

    rbac.grant_network_contributor_on_subnet(
        object(),
        "sub-1",
        principal_id="cluster-principal",
        subnet_id="/subs/sub-1/.../subnets/snet-aks",
    )

    assert len(created) == 1
    assert created[0]["scope"] == "/subs/sub-1/.../subnets/snet-aks"
    assert created[0]["principal_id"] == "cluster-principal"
    assert created[0]["role_definition_id"].endswith(
        "4d97b98b-1d4f-4787-a291-c67834d212e7"
    )


def test_grant_network_contributor_on_subnet_noop_on_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import api.tasks.azure.rbac as rbac

    def _boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("AuthorizationManagementClient must not be constructed")

    monkeypatch.setattr(
        "azure.mgmt.authorization.AuthorizationManagementClient", _boom
    )

    rbac.grant_network_contributor_on_subnet(
        object(), "sub-1", principal_id="", subnet_id="/x"
    )
    rbac.grant_network_contributor_on_subnet(
        object(), "sub-1", principal_id="p", subnet_id=""
    )



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


def test_provision_aks_step_counter_is_monotonic_with_pre_create_rbac(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The banner step counter must never go backwards.

    The pre-create dashboard-MI self-grant runs BEFORE the ARM create
    (step 3) but reuses the `_RBAC_SUB_PHASES` strings, which map to the
    post-create RBAC step (4). Before the fix, those pre-create ticks
    published step 4, so the banner showed 2 -> 4 -> 3 (ARM) -> 4 -> 5.
    This test fires the pre-create progress callback and asserts every
    published `step` is monotonically non-decreasing.
    """
    import api.tasks.azure as azure
    from api.tasks.azure import provision as provision_mod
    from api.tasks.azure import provision_aks

    class FakeResourceGroups:
        def create_or_update(self, *_args: Any, **_kwargs: Any) -> object:
            return object()

        def get(self, _rg: str) -> object:
            return object()

    class FakeRc:
        resource_groups = FakeResourceGroups()

    class FakePoller:
        def done(self) -> bool:
            return True

        def result(self) -> object:
            class _Cluster:
                identity = type("I", (), {"principal_id": "mi-principal"})()
                provisioning_state = "Succeeded"
                node_resource_group = "MC_rg_x_koreacentral"

            return _Cluster()

    class FakeManagedClusters:
        def begin_create_or_update(self, _rg: str, _name: str, _params: object) -> FakePoller:
            return FakePoller()

        def get(self, _rg: str, _name: str) -> object:
            class _C:
                provisioning_state = "Creating"

            return _C()

    class FakeAgentPools:
        def list(self, _rg: str, _cluster: str) -> list[object]:
            return []

    class FakeAksClient:
        managed_clusters = FakeManagedClusters()
        agent_pools = FakeAgentPools()

    publishes: list[dict[str, Any]] = []

    def _capture(state: str, meta: dict[str, Any] | None = None, **_kw: Any) -> None:
        publishes.append({"state": state, "meta": dict(meta or {})})

    monkeypatch.setattr(provision_aks, "update_state", _capture)
    monkeypatch.setattr(provision_mod.time, "sleep", lambda _secs: None)
    monkeypatch.setattr(azure, "get_credential", lambda: object())
    monkeypatch.setattr(azure, "aks_client", lambda _cred, _sub: FakeAksClient())
    monkeypatch.setattr(azure, "resource_client", lambda _cred, _sub: FakeRc())

    # Fire the pre-create RBAC progress callback with a sub-phase that maps
    # to the post-create step (4) via `_RBAC_SUB_PHASES`. The fix must pin
    # these ticks to the RG step (2) instead.
    def _fake_pre_create(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        cb = kwargs.get("progress_callback")
        if cb is not None:
            cb("ensuring_dashboard_mi_rbac", "Self-granting dashboard MI on cluster RG")
        return {"roles_assigned": ["Contributor"], "roles_failed": []}

    monkeypatch.setattr(azure, "_ensure_dashboard_mi_cluster_rg_roles", _fake_pre_create)
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
        job_id="job-monotonic",
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

    # The pre-create RBAC tick (published BEFORE the ARM create) must land
    # on the RG step (2), not the post-create RBAC step (4). The same
    # sub-phase string is also published post-create at step 4 — that one
    # is legitimate because it runs after ARM (step 3).
    phases_in_order = [p["meta"].get("phase") for p in publishes if p["state"] == "PROGRESS"]
    arm_idx = phases_in_order.index("arm_create_or_update")
    pre_create_ticks = [
        p["meta"]
        for i, p in enumerate(
            [pp for pp in publishes if pp["state"] == "PROGRESS"]
        )
        if p["meta"].get("phase") == "ensuring_dashboard_mi_rbac" and i < arm_idx
    ]
    assert pre_create_ticks, publishes
    assert all(t["step"] == 2 for t in pre_create_ticks), pre_create_ticks

    # No PROGRESS publish may report a step lower than a previous one.
    steps = [
        p["meta"]["step"]
        for p in publishes
        if p["state"] == "PROGRESS" and isinstance(p["meta"].get("step"), int)
    ]
    assert steps == sorted(steps), steps


def test_provision_aks_retries_in_progress_cluster_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import api.tasks.azure as azure
    from api.tasks.azure import provision_aks
    from api.tasks.azure.provision import _AKS_OPERATION_CONFLICT_RETRY_SECONDS
    from azure.core.exceptions import ResourceExistsError

    class RetryRequested(Exception):
        pass

    class FakeResourceGroups:
        def get(self, _rg: str) -> object:
            return object()

    class FakeRc:
        resource_groups = FakeResourceGroups()

    class FakeManagedClusters:
        def begin_create_or_update(self, _rg: str, _name: str, _params: object) -> object:
            raise ResourceExistsError(
                message=(
                    "(OperationNotAllowed) Operation is not allowed because there's an "
                    "in progress create managed cluster operation."
                )
            )

    class FakeAksClient:
        managed_clusters = FakeManagedClusters()

    publishes: list[dict[str, Any]] = []
    retry_calls: list[dict[str, Any]] = []

    def _capture(state: str, meta: dict[str, Any] | None = None, **_kw: Any) -> None:
        publishes.append({"state": state, "meta": dict(meta or {})})

    def _retry(**kwargs: Any) -> None:
        retry_calls.append(kwargs)
        raise RetryRequested()

    monkeypatch.setattr(provision_aks, "update_state", _capture)
    monkeypatch.setattr(provision_aks, "retry", _retry)
    monkeypatch.setattr(azure, "get_credential", lambda: object())
    monkeypatch.setattr(azure, "aks_client", lambda _cred, _sub: FakeAksClient())
    monkeypatch.setattr(azure, "resource_client", lambda _cred, _sub: FakeRc())

    with pytest.raises(RetryRequested):
        provision_aks.run(
            job_id="job-conflict",
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

    assert retry_calls
    assert retry_calls[0]["countdown"] == _AKS_OPERATION_CONFLICT_RETRY_SECONDS
    phases = [p["meta"].get("phase") for p in publishes]
    assert phases[-1] == "arm_create_or_update"
    assert publishes[-1]["meta"]["status"] == "running"
    assert publishes[-1]["meta"]["error_code"] == "aks_operation_in_progress"
    assert "failed" not in phases


def test_provision_aks_includes_dashboard_mi_rbac_in_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The completed task payload + final PROGRESS publish must include
    `dashboard_mi_rbac` so the SPA can show whether the cluster-RG
    self-grant succeeded or surface the recovery command on failure.

    This is the integration assertion for Part A of the OpenAPI-deploy
    RBAC-gap fix. Without it, a regression that silently drops the
    self-grant step would slip through the existing happy-path tests.
    """
    import api.tasks.azure as azure
    from api.tasks.azure import provision_aks
    from azure.core.exceptions import ResourceNotFoundError

    class FakeResourceGroups:
        def create_or_update(self, *_args: Any, **_kwargs: Any) -> object:
            return object()

        def get(self, _rg: str) -> object:
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
            self, _rg: str, _name: str, _params: object
        ) -> FakePoller:
            return FakePoller()

    class FakeAksClient:
        managed_clusters = FakeManagedClusters()

    publishes: list[dict[str, Any]] = []

    def _capture(state: str, meta: dict[str, Any] | None = None, **_kw: Any) -> None:
        publishes.append({"state": state, "meta": dict(meta or {})})

    monkeypatch.setattr(provision_aks, "update_state", _capture)
    monkeypatch.setattr(azure, "get_credential", lambda: object())
    monkeypatch.setattr(azure, "aks_client", lambda _cred, _sub: FakeAksClient())
    monkeypatch.setattr(azure, "resource_client", lambda _cred, _sub: FakeRc())
    monkeypatch.setattr(
        azure,
        "_ensure_aks_runtime_rbac",
        lambda *_args, **_kwargs: {
            "roles_assigned": ["AcrPull"],
            "roles_failed": [],
        },
    )
    # Mark the self-grant as "skipped" (the simplest deterministic
    # outcome for this integration test) by clearing the env var. The
    # behaviour-specific paths (success / failure / idempotent) are
    # exercised in `test_azure_tasks.py::test_ensure_dashboard_mi_*`.
    monkeypatch.delenv("SHARED_IDENTITY_PRINCIPAL_ID", raising=False)

    result = provision_aks.run(
        job_id="job-mi-rbac",
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

    assert "dashboard_mi_rbac" in result
    assert result["dashboard_mi_rbac"].get("skipped") is True

    # The final `completed` publish must also carry it so the SPA can
    # render the recovery affordance from the progress stream alone
    # (without waiting for `/api/tasks/{id}` result).
    completed = [p for p in publishes if p["meta"].get("phase") == "completed"]
    assert completed, publishes
    assert "dashboard_mi_rbac" in completed[-1]["meta"]

    _ = ResourceNotFoundError  # silence import-unused
