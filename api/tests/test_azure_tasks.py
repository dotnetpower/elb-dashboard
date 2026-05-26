"""Tests for Azure Tasks behavior.

Responsibility: Tests for Azure Tasks behavior
Edit boundaries: Keep assertions focused on the behavior under test; prefer fakes over live
Azure calls.
Key entry points: `test_attach_acr_uses_subscription_scoped_role_definition`,
`test_grant_storage_blob_contributor_uses_storage_scope`,
`test_ensure_aks_runtime_rbac_grants_acr_and_storage`,
`test_ensure_aks_runtime_rbac_reports_nonfatal_failures`,
`test_start_aks_enqueues_openapi_after_cluster_start`
Risky contracts: Do not require network access or real Azure credentials unless the test is
explicitly integration-scoped.
Validation: `uv run pytest -q api/tests/test_azure_tasks.py`.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from api.tasks import azure
from api.tests._fakes import AsyncResultStub


def test_attach_acr_uses_subscription_scoped_role_definition(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    class FakeAksClient:
        managed_clusters = SimpleNamespace(
            get=lambda _resource_group, _cluster_name: SimpleNamespace(
                identity_profile={"kubeletidentity": SimpleNamespace(object_id="kubelet-oid")}
            )
        )

    class FakeAcrClient:
        registries = SimpleNamespace(
            get=lambda _resource_group, _name: SimpleNamespace(
                id="/subscriptions/sub-1/resourceGroups/rg-acr/providers/Microsoft.ContainerRegistry/registries/acr1"
            )
        )

    class FakeRoleAssignments:
        def create(
            self, *, scope: str, role_assignment_name: str, parameters: dict[str, Any]
        ) -> None:
            captured["scope"] = scope
            captured["role_assignment_name"] = role_assignment_name
            captured["parameters"] = parameters

    class FakeAuthorizationClient:
        def __init__(self, _cred: object, _subscription_id: str) -> None:
            self.role_assignments = FakeRoleAssignments()

    import azure.mgmt.authorization as auth_mod

    monkeypatch.setattr(azure, "aks_client", lambda _cred, _sub: FakeAksClient())
    monkeypatch.setattr(azure, "acr_client", lambda _cred, _sub: FakeAcrClient())
    monkeypatch.setattr(auth_mod, "AuthorizationManagementClient", FakeAuthorizationClient)

    azure._attach_acr(
        object(),
        "sub-1",
        "rg-aks",
        "aks1",
        "rg-acr",
        "acr1",
    )

    assert captured["scope"].endswith("/registries/acr1")
    params = captured["parameters"]
    assert params.principal_id == "kubelet-oid"
    assert params.role_definition_id == (
        "/subscriptions/sub-1/providers/Microsoft.Authorization/roleDefinitions/"
        "7f951dda-4ed3-4680-a7ca-43fe172d538d"
    )


def test_grant_storage_blob_contributor_uses_storage_scope(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    class FakeAksClient:
        managed_clusters = SimpleNamespace(
            get=lambda _resource_group, _cluster_name: SimpleNamespace(
                identity_profile={"kubeletidentity": SimpleNamespace(object_id="kubelet-oid")}
            )
        )

    class FakeStorageAccounts:
        def get_properties(self, _resource_group: str, _account_name: str) -> SimpleNamespace:
            return SimpleNamespace(
                id="/subscriptions/sub-1/resourceGroups/rg-storage/providers/Microsoft.Storage/storageAccounts/stg1"
            )

    class FakeStorageClient:
        storage_accounts = FakeStorageAccounts()

    class FakeRoleAssignments:
        def create(self, *, scope: str, role_assignment_name: str, parameters: object) -> None:
            captured["scope"] = scope
            captured["role_assignment_name"] = role_assignment_name
            captured["parameters"] = parameters

    class FakeAuthorizationClient:
        def __init__(self, _cred: object, _subscription_id: str) -> None:
            self.role_assignments = FakeRoleAssignments()

    import azure.mgmt.authorization as auth_mod

    monkeypatch.setattr(azure, "aks_client", lambda _cred, _sub: FakeAksClient())
    monkeypatch.setattr(azure, "storage_client", lambda _cred, _sub: FakeStorageClient())
    monkeypatch.setattr(auth_mod, "AuthorizationManagementClient", FakeAuthorizationClient)

    azure._grant_storage_blob_contributor_to_aks(
        object(),
        "sub-1",
        "rg-aks",
        "aks1",
        "rg-storage",
        "stg1",
    )

    assert captured["scope"].endswith("/storageAccounts/stg1")
    params = captured["parameters"]
    assert params.principal_id == "kubelet-oid"
    assert params.role_definition_id == (
        "/subscriptions/sub-1/providers/Microsoft.Authorization/roleDefinitions/"
        "ba92f5b4-2d11-453d-a403-e96b0029c9fe"
    )


def test_ensure_aks_runtime_rbac_grants_acr_and_storage(monkeypatch) -> None:
    calls: list[tuple[str, str, str]] = []

    def fake_attach_acr(
        _cred: object,
        _subscription_id: str,
        _resource_group: str,
        _cluster_name: str,
        acr_resource_group: str,
        acr_name: str,
    ) -> None:
        calls.append(("acr", acr_resource_group, acr_name))

    def fake_grant_storage(
        _cred: object,
        _subscription_id: str,
        _resource_group: str,
        _cluster_name: str,
        storage_resource_group: str,
        storage_account: str,
    ) -> None:
        calls.append(("storage", storage_resource_group, storage_account))

    monkeypatch.setattr(azure, "_attach_acr", fake_attach_acr)
    monkeypatch.setattr(azure, "_grant_storage_blob_contributor_to_aks", fake_grant_storage)
    # Provide a kubelet OID so the function reaches the grant helpers
    # instead of taking the kubelet-missing early-exit branch.
    import api.tasks.azure.rbac as rbac_mod
    monkeypatch.setattr(rbac_mod, "_resolve_kubelet_oid", lambda *_a, **_kw: "kubelet-oid")

    summary = azure._ensure_aks_runtime_rbac(
        object(),
        "sub-1",
        "rg-aks",
        "aks1",
        acr_resource_group="rg-acr",
        acr_name="acr1",
        storage_resource_group="rg-storage",
        storage_account="stg1",
    )

    assert calls == [("acr", "rg-acr", "acr1"), ("storage", "rg-storage", "stg1")]
    assert summary["roles_assigned"] == ["AcrPull", "Storage Blob Data Contributor"]
    assert summary["roles_failed"] == {}


def test_ensure_aks_runtime_rbac_reports_nonfatal_failures(monkeypatch) -> None:
    def fail_attach_acr(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("missing roleAssignments/write")

    monkeypatch.setattr(azure, "_attach_acr", fail_attach_acr)
    monkeypatch.setattr(
        azure, "_grant_storage_blob_contributor_to_aks", lambda *_args, **_kwargs: None
    )
    # Provide a kubelet OID so we reach the grant helpers (otherwise the
    # kubelet-missing early-exit would short-circuit before fail_attach_acr).
    import api.tasks.azure.rbac as rbac_mod
    monkeypatch.setattr(rbac_mod, "_resolve_kubelet_oid", lambda *_a, **_kw: "kubelet-oid")

    summary = azure._ensure_aks_runtime_rbac(
        object(),
        "sub-1",
        "rg-aks",
        "aks1",
        acr_resource_group="rg-acr",
        acr_name="acr1",
        storage_resource_group="rg-storage",
        storage_account="stg1",
    )

    assert summary["roles_assigned"] == ["Storage Blob Data Contributor"]
    assert "AcrPull" in summary["roles_failed"]


def test_ensure_aks_runtime_rbac_publishes_sub_phases(monkeypatch) -> None:
    """The provision banner shows per-role sub-phases ("Granting AcrPull
    ...", "Granting Storage Blob ...") under the parent step. The helper
    must invoke the optional progress_callback for every grant target."""
    monkeypatch.setattr(azure, "_attach_acr", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        azure, "_grant_storage_blob_contributor_to_aks", lambda *_a, **_kw: None
    )
    import api.tasks.azure.rbac as rbac_mod
    monkeypatch.setattr(rbac_mod, "_resolve_kubelet_oid", lambda *_a, **_kw: "kubelet-oid")

    published: list[tuple[str, str]] = []

    azure._ensure_aks_runtime_rbac(
        object(),
        "sub-1",
        "rg-aks",
        "aks1",
        acr_resource_group="rg-acr",
        acr_name="acr1",
        storage_resource_group="rg-storage",
        storage_account="stg1",
        progress_callback=lambda phase, msg: published.append((phase, msg)),
    )

    phases = [p for p, _ in published]
    assert phases == ["ensuring_rbac_acr", "ensuring_rbac_storage"]
    # The message must mention the actual resource so the user sees what
    # is being granted instead of a generic "Granting roles".
    assert "acr1" in published[0][1]
    assert "stg1" in published[1][1]


def test_ensure_aks_runtime_rbac_does_not_swallow_internal_typeerror(monkeypatch) -> None:
    """Regression: the legacy-signature fallback caught the broad
    ``TypeError``, so a genuine TypeError raised from inside
    ``_attach_acr`` (e.g. a malformed RoleAssignmentCreateParameters) was
    silently retried as if it were just a signature mismatch. The
    narrowed fallback must propagate non-`kubelet_oid` TypeErrors so the
    role lands in ``roles_failed`` and provision fails-fast."""
    sentinel = TypeError("RoleAssignmentCreateParameters() got an unexpected keyword 'foo'")

    def boom_attach_acr(
        _cred: object,
        _sub: str,
        _rg: str,
        _cluster: str,
        _acr_rg: str,
        _acr_name: str,
        *,
        kubelet_oid: str | None = None,
    ) -> None:
        # Accepts the kubelet_oid kwarg so the fallback path is NOT taken;
        # the TypeError originates inside the body, not from the signature.
        raise sentinel

    monkeypatch.setattr(azure, "_attach_acr", boom_attach_acr)
    monkeypatch.setattr(
        azure, "_grant_storage_blob_contributor_to_aks", lambda *_a, **_kw: None
    )
    import api.tasks.azure.rbac as rbac_mod
    monkeypatch.setattr(rbac_mod, "_resolve_kubelet_oid", lambda *_a, **_kw: "kubelet-oid")

    summary = azure._ensure_aks_runtime_rbac(
        object(),
        "sub-1",
        "rg-aks",
        "aks1",
        acr_resource_group="rg-acr",
        acr_name="acr1",
    )
    # Genuine TypeError surfaced as a failed role, not silently retried.
    assert "AcrPull" in summary["roles_failed"]


def test_ensure_aks_runtime_rbac_fails_when_kubelet_oid_missing(monkeypatch) -> None:
    """Regression: a cluster with no kubelet managed identity (legacy
    service-principal mode, or an interrupted create) used to make every
    grant return silently — `roles_failed` stayed empty and the provision
    task happily marked the cluster "Cluster ready" with NOTHING assigned.

    With the fix, the absence of a kubelet OID surfaces as an explicit
    failure for every configured target so the provision task fail-fasts
    instead of leaving a half-broken cluster behind."""
    import api.tasks.azure.rbac as rbac_mod

    # Force the OID lookup to report "no kubelet identity" by returning
    # None (this is the genuine cluster-shape case, distinct from a
    # lookup exception). The grant helpers must NOT be called at all
    # in this branch.
    monkeypatch.setattr(rbac_mod, "_resolve_kubelet_oid", lambda *_a, **_kw: None)
    called: list[str] = []
    monkeypatch.setattr(
        azure, "_attach_acr", lambda *_a, **_kw: called.append("acr")
    )
    monkeypatch.setattr(
        azure,
        "_grant_storage_blob_contributor_to_aks",
        lambda *_a, **_kw: called.append("storage"),
    )

    summary = azure._ensure_aks_runtime_rbac(
        object(),
        "sub-1",
        "rg-aks",
        "aks1",
        acr_resource_group="rg-acr",
        acr_name="acr1",
        storage_resource_group="rg-storage",
        storage_account="stg1",
    )

    assert summary["roles_assigned"] == []
    assert "AcrPull" in summary["roles_failed"]
    assert "Storage Blob Data Contributor" in summary["roles_failed"]
    assert "kubelet" in summary["roles_failed"]["AcrPull"].lower()
    # Grant helpers must not be touched — otherwise the "skip on missing
    # OID" path inside them would have masked the failure again.
    assert called == []


def test_ensure_aks_runtime_rbac_skips_kubelet_check_when_no_targets(monkeypatch) -> None:
    """No ACR / Storage configured → the kubelet-missing branch must not
    spuriously emit failures. The function should return an empty summary
    so callers see "nothing to do" instead of "everything failed"."""
    import api.tasks.azure.rbac as rbac_mod

    monkeypatch.setattr(rbac_mod, "_resolve_kubelet_oid", lambda *_a, **_kw: None)

    summary = azure._ensure_aks_runtime_rbac(
        object(),
        "sub-1",
        "rg-aks",
        "aks1",
    )

    assert summary["roles_assigned"] == []
    assert summary["roles_failed"] == {}


def test_ensure_aks_runtime_rbac_defaults_storage_from_env(monkeypatch) -> None:
    """Regression for the silent-skip that produced `AKS cache failed`
    with `azcopy ... 403 AuthorizationPermissionMismatch` during warmup.

    The SPA cluster-provision form historically left `storage_account`
    empty in the request body. The provision route forwarded `""` to
    `ensure_aks_runtime_rbac`, which then skipped the Storage Blob Data
    Contributor grant entirely — the kubelet ended up with `AcrPull`
    only, and every warmup `azcopy cp` later 403'd on the manifest HEAD.

    With the env-default in place, when the caller omits the target and
    the platform env carries the workload Storage identifier, the grant
    helper is invoked with those values so the kubelet always gets the
    role on the dashboard's own Storage account."""
    monkeypatch.setenv("STORAGE_ACCOUNT_NAME", "stworkload")
    monkeypatch.setenv("AZURE_RESOURCE_GROUP", "rg-workload")

    import api.tasks.azure.rbac as rbac_mod

    monkeypatch.setattr(rbac_mod, "_resolve_kubelet_oid", lambda *_a, **_kw: "kubelet-oid")
    monkeypatch.setattr(azure, "_attach_acr", lambda *_a, **_kw: None)

    grants: list[tuple[str, str]] = []

    def fake_grant_storage(
        _cred: object,
        _subscription_id: str,
        _resource_group: str,
        _cluster_name: str,
        storage_resource_group: str,
        storage_account: str,
    ) -> None:
        grants.append((storage_resource_group, storage_account))

    monkeypatch.setattr(azure, "_grant_storage_blob_contributor_to_aks", fake_grant_storage)

    summary = azure._ensure_aks_runtime_rbac(
        object(),
        "sub-1",
        "rg-aks",
        "aks1",
        acr_resource_group="rg-acr",
        acr_name="acr1",
        # SPA omits storage target — the env default must fill it.
        storage_resource_group="",
        storage_account="",
    )

    assert grants == [("rg-workload", "stworkload")]
    assert "Storage Blob Data Contributor" in summary["roles_assigned"]
    assert summary["roles_failed"] == {}


def test_create_role_assignment_retries_principal_not_found(monkeypatch) -> None:
    """Freshly minted kubelet identities occasionally hit
    ``PrincipalNotFound`` for ~30 s while Entra ID propagates. The retry
    helper must absorb that case without raising."""
    import api.tasks.azure.rbac as rbac

    monkeypatch.setattr(rbac.time, "sleep", lambda _s: None)
    attempts = {"n": 0}

    class _FakeRoleAssignments:
        def create(
            self,
            *,
            scope: str,
            role_assignment_name: str,
            parameters: object,
        ) -> None:
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise RuntimeError(
                    "PrincipalNotFound: Principal a-b-c does not exist in the directory"
                )
            return None

    class _FakeAuth:
        role_assignments = _FakeRoleAssignments()

    rbac._create_role_assignment_with_retry(
        _FakeAuth(),
        "/subscriptions/sub-1/resourceGroups/rg/providers/X/registries/r",
        "name-1",
        object(),
        label="AcrPull",
    )
    assert attempts["n"] == 3


def test_create_role_assignment_treats_conflict_as_idempotent(monkeypatch) -> None:
    """``RoleAssignmentExists`` / ``Conflict`` is a re-run case (existing
    cluster), not a failure. The helper must return silently."""
    import api.tasks.azure.rbac as rbac

    monkeypatch.setattr(rbac.time, "sleep", lambda _s: None)

    class _FakeRoleAssignments:
        def create(self, *, scope: str, role_assignment_name: str, parameters: object) -> None:
            raise RuntimeError("RoleAssignmentExists: assignment already present")

    class _FakeAuth:
        role_assignments = _FakeRoleAssignments()

    rbac._create_role_assignment_with_retry(
        _FakeAuth(),
        "/sub/scope",
        "name-1",
        object(),
        label="AcrPull",
    )


def test_assign_aks_roles_task_raises_on_failed_roles(monkeypatch) -> None:
    """The standalone ``assign_aks_roles`` task (Re-assign roles button)
    must raise when at least one role failed, so the SPA polling
    ``/api/tasks/{id}`` sees ``FAILURE`` instead of ``status:completed``
    with a quiet ``roles_failed[]``."""
    monkeypatch.setattr(azure, "get_credential", lambda: object())
    monkeypatch.setattr(
        azure,
        "_ensure_aks_runtime_rbac",
        lambda *_a, **_kw: {
            "cluster_name": "aks1",
            "roles_assigned": [],
            "roles_failed": {"AcrPull": "auth denied"},
        },
    )

    import pytest as _pytest

    with _pytest.raises(RuntimeError, match="Failed to assign runtime RBAC"):
        azure.assign_aks_roles.run(
            subscription_id="sub-1",
            resource_group="rg-aks",
            cluster_name="aks1",
            acr_resource_group="rg-acr",
            acr_name="acr1",
        )


def test_start_aks_enqueues_openapi_after_cluster_start(monkeypatch) -> None:
    sent_tasks: list[dict[str, Any]] = []

    class FakePoller:
        def result(self) -> None:
            return None

    class FakeManagedClusters:
        def begin_start(self, resource_group: str, cluster_name: str) -> FakePoller:
            sent_tasks.append({"started": f"{resource_group}/{cluster_name}"})
            return FakePoller()

    class FakeAksClient:
        managed_clusters = FakeManagedClusters()

    def fake_send_task(
        task_name: str,
        *,
        kwargs: dict[str, Any],
        queue: str | None = None,
    ) -> AsyncResultStub:
        sent_tasks.append({"task_name": task_name, "kwargs": kwargs, "queue": queue})
        return AsyncResultStub("task-openapi-123")

    monkeypatch.setattr(azure, "get_credential", lambda: object())
    monkeypatch.setattr(azure, "aks_client", lambda _cred, _sub: FakeAksClient())
    monkeypatch.setattr("api.celery_app.celery_app.send_task", fake_send_task)

    result = azure.start_aks.run(
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="elb-cluster",
        auto_openapi={
            "acr_name": "elbacr01",
            "acr_resource_group": "rg-elbacr",
            "storage_account": "elbstg01",
            "storage_resource_group": "rg-storage",
        },
    )

    assert result["openapi_task_id"] == "task-openapi-123"
    assert sent_tasks[0] == {"started": "rg-elb/elb-cluster"}
    assert sent_tasks[1]["task_name"] == "api.tasks.openapi.deploy_openapi_service"
    assert sent_tasks[1]["queue"] == "azure"
    assert sent_tasks[1]["kwargs"] == {
        "subscription_id": "sub-1",
        "resource_group": "rg-elb",
        "cluster_name": "elb-cluster",
        "acr_name": "elbacr01",
        "acr_resource_group": "rg-elbacr",
        "storage_account": "elbstg01",
        "storage_resource_group": "rg-storage",
    }


class _FakePoller:
    def result(self) -> None:
        return None


class _FakeDeleteAks:
    def __init__(self) -> None:
        self.deleted: list[tuple[str, str]] = []

    def begin_delete(self, resource_group: str, cluster_name: str) -> _FakePoller:
        self.deleted.append((resource_group, cluster_name))
        return _FakePoller()


def _install_fake_aks(monkeypatch) -> _FakeDeleteAks:
    fake_mc = _FakeDeleteAks()

    class FakeAksClient:
        managed_clusters = fake_mc

    monkeypatch.setattr(azure, "get_credential", lambda: object())
    monkeypatch.setattr(azure, "aks_client", lambda _cred, _sub: FakeAksClient())
    return fake_mc


def test_delete_aks_removes_empty_resource_group(monkeypatch) -> None:
    fake_mc = _install_fake_aks(monkeypatch)
    rg_deleted: list[str] = []

    class FakeResources:
        def list_by_resource_group(self, _rg: str):
            return iter(())  # empty after AKS removal

    class FakeResourceGroups:
        def get(self, _rg: str) -> SimpleNamespace:
            # Tag proves we created this RG via provision_aks, so we own
            # the cleanup. Without this tag the RG is treated as user-
            # owned and left untouched.
            return SimpleNamespace(tags={"managed-by": "elb-dashboard"})

        def begin_delete(self, rg: str) -> _FakePoller:
            rg_deleted.append(rg)
            return _FakePoller()

    class FakeResourceClient:
        resources = FakeResources()
        resource_groups = FakeResourceGroups()

    monkeypatch.setattr(azure, "resource_client", lambda _cred, _sub: FakeResourceClient())

    result = azure.delete_aks.run(
        subscription_id="sub-1",
        resource_group="rg-elb-cluster",
        cluster_name="elb-cluster",
    )

    assert fake_mc.deleted == [("rg-elb-cluster", "elb-cluster")]
    assert rg_deleted == ["rg-elb-cluster"]
    assert result["resource_group_status"] == "deleted"
    assert result["resource_group_remaining"] == 0
    assert result["status"] == "completed"


def test_delete_aks_keeps_resource_group_with_other_resources(monkeypatch) -> None:
    fake_mc = _install_fake_aks(monkeypatch)
    rg_deleted: list[str] = []

    class FakeResources:
        def list_by_resource_group(self, _rg: str):
            return iter(
                [
                    SimpleNamespace(name="stelbprod"),
                    SimpleNamespace(name="acrelbprod"),
                ]
            )

    class FakeResourceGroups:
        def get(self, _rg: str) -> SimpleNamespace:
            return SimpleNamespace(tags={"managed-by": "elb-dashboard"})

        def begin_delete(self, rg: str) -> _FakePoller:
            rg_deleted.append(rg)
            return _FakePoller()

    class FakeResourceClient:
        resources = FakeResources()
        resource_groups = FakeResourceGroups()

    monkeypatch.setattr(azure, "resource_client", lambda _cred, _sub: FakeResourceClient())

    result = azure.delete_aks.run(
        subscription_id="sub-1",
        resource_group="rg-elb-shared",
        cluster_name="aks-shared",
    )

    assert fake_mc.deleted == [("rg-elb-shared", "aks-shared")]
    assert rg_deleted == []
    assert result["resource_group_status"] == "retained_not_empty"
    assert result["resource_group_remaining"] == 2


def test_delete_aks_rg_cleanup_failure_does_not_fail_task(monkeypatch) -> None:
    _install_fake_aks(monkeypatch)

    def boom(_cred, _sub):
        raise RuntimeError("ARM down for maintenance")

    monkeypatch.setattr(azure, "resource_client", boom)

    result = azure.delete_aks.run(
        subscription_id="sub-1",
        resource_group="rg-elb-cluster",
        cluster_name="elb-cluster",
    )

    assert result["status"] == "completed"
    assert result["resource_group_status"] == "error"
    assert result["resource_group_remaining"] == -1


def test_delete_aks_retains_user_owned_rg_without_managed_tag(monkeypatch) -> None:
    """Regression: an RG without our ownership tag (e.g. created by the
    user before this dashboard was deployed, or by a sibling tool) must
    NEVER be auto-deleted, even when it is empty after the AKS removal.
    Closes the TOCTOU + accidental-deletion class of bugs at the same time
    — only RGs `provision_aks` itself tagged are candidates for cleanup.
    """
    fake_mc = _install_fake_aks(monkeypatch)
    rg_deleted: list[str] = []

    class FakeResources:
        def list_by_resource_group(self, _rg: str):
            # If the gate ever runs this list and reaches begin_delete it
            # would still appear empty — making the test assert that the
            # tag gate (not the empty check) is what stops us.
            return iter(())

    class FakeResourceGroups:
        def get(self, _rg: str) -> SimpleNamespace:
            # No managed-by tag → treated as user-owned.
            return SimpleNamespace(tags={"environment": "prod"})

        def begin_delete(self, rg: str) -> _FakePoller:
            rg_deleted.append(rg)
            return _FakePoller()

    class FakeResourceClient:
        resources = FakeResources()
        resource_groups = FakeResourceGroups()

    monkeypatch.setattr(azure, "resource_client", lambda _cred, _sub: FakeResourceClient())

    result = azure.delete_aks.run(
        subscription_id="sub-1",
        resource_group="rg-user-shared",
        cluster_name="elb-aks",
    )

    assert fake_mc.deleted == [("rg-user-shared", "elb-aks")]
    # The critical assertion: even though the list was empty, the RG
    # delete must NOT have been called because our ownership tag is
    # missing. This is what closes the TOCTOU window.
    assert rg_deleted == []
    assert result["resource_group_status"] == "retained_not_owned"


def test_delete_aks_accepts_managedby_camelcase_tag(monkeypatch) -> None:
    """Older RGs may carry `managedBy` (camelCase) instead of the modern
    kebab-case `managed-by`. Both spellings prove ownership."""
    fake_mc = _install_fake_aks(monkeypatch)
    rg_deleted: list[str] = []

    class FakeResources:
        def list_by_resource_group(self, _rg: str):
            return iter(())

    class FakeResourceGroups:
        def get(self, _rg: str) -> SimpleNamespace:
            return SimpleNamespace(tags={"managedBy": "elb-dashboard"})

        def begin_delete(self, rg: str) -> _FakePoller:
            rg_deleted.append(rg)
            return _FakePoller()

    class FakeResourceClient:
        resources = FakeResources()
        resource_groups = FakeResourceGroups()

    monkeypatch.setattr(azure, "resource_client", lambda _cred, _sub: FakeResourceClient())

    result = azure.delete_aks.run(
        subscription_id="sub-1",
        resource_group="rg-elb-legacy",
        cluster_name="elb-aks",
    )

    assert fake_mc.deleted == [("rg-elb-legacy", "elb-aks")]
    assert rg_deleted == ["rg-elb-legacy"]
    assert result["resource_group_status"] == "deleted"


# ---------------------------------------------------------------------------
# Dashboard MI self-grant on the AKS cluster RG (Part A of the OpenAPI-deploy
# RBAC-gap fix). See `api/tasks/azure/rbac.py::ensure_dashboard_mi_cluster_rg_roles`.
# ---------------------------------------------------------------------------


def test_ensure_dashboard_mi_cluster_rg_roles_skips_when_no_principal_id(
    monkeypatch,
) -> None:
    """Local-dev shell with no `SHARED_IDENTITY_PRINCIPAL_ID` must skip
    silently — there is no MI to self-grant to, and the helper must not
    blow up by hitting the Azure SDK.
    """
    monkeypatch.delenv("SHARED_IDENTITY_PRINCIPAL_ID", raising=False)

    result = azure._ensure_dashboard_mi_cluster_rg_roles(
        object(),
        subscription_id="sub-1",
        cluster_resource_group="rg-elb-cluster",
        mi_principal_id="",
    )

    assert result == {
        "skipped": True,
        "reason": "SHARED_IDENTITY_PRINCIPAL_ID not set",
        "roles_assigned": [],
        "roles_failed": {},
    }


def test_ensure_dashboard_mi_cluster_rg_roles_assigns_both_roles(monkeypatch) -> None:
    """Happy path: Contributor + UAA assigned on cluster RG to the MI.

    Verifies the helper writes the role assignments at RG scope with the
    well-known role definition GUIDs and returns both labels in
    `roles_assigned` plus the recovery command for SPA display.
    """
    captured: list[dict[str, Any]] = []

    class FakeRoleAssignments:
        def create(
            self, *, scope: str, role_assignment_name: str, parameters: object
        ) -> None:
            captured.append(
                {
                    "scope": scope,
                    "role_assignment_name": role_assignment_name,
                    "role_definition_id": parameters.role_definition_id,
                    "principal_id": parameters.principal_id,
                    "principal_type": parameters.principal_type,
                }
            )

    class FakeAuthorizationClient:
        def __init__(self, _cred: object, _sub: str) -> None:
            self.role_assignments = FakeRoleAssignments()

    import azure.mgmt.authorization as auth_mod

    monkeypatch.setattr(auth_mod, "AuthorizationManagementClient", FakeAuthorizationClient)

    result = azure._ensure_dashboard_mi_cluster_rg_roles(
        object(),
        subscription_id="sub-1",
        cluster_resource_group="rg-elb-cluster",
        mi_principal_id="mi-oid",
    )

    assert result["roles_assigned"] == ["Contributor", "User Access Administrator"]
    assert result["roles_failed"] == {}
    assert result["mi_principal_id"] == "mi-oid"
    assert result["cluster_resource_group"] == "rg-elb-cluster"
    assert "grant-runtime-rbac.sh" in result["recovery_command"]
    assert "--cluster-rg rg-elb-cluster" in result["recovery_command"]
    assert "--principal-id mi-oid" in result["recovery_command"]

    assert len(captured) == 2
    rg_scope = "/subscriptions/sub-1/resourceGroups/rg-elb-cluster"
    assert all(c["scope"] == rg_scope for c in captured), captured
    assert all(c["principal_id"] == "mi-oid" for c in captured), captured
    assert all(c["principal_type"] == "ServicePrincipal" for c in captured), captured
    role_guids = sorted(c["role_definition_id"].rsplit("/", 1)[-1] for c in captured)
    assert role_guids == sorted(
        [
            "b24988ac-6180-42a0-ab88-20f7382dd24c",  # Contributor
            "18d7d88d-d35e-4fb5-a5c3-7773c20a72d9",  # User Access Administrator
        ]
    )


def test_ensure_dashboard_mi_cluster_rg_roles_records_authorization_failure(
    monkeypatch,
) -> None:
    """When the MI lacks `roleAssignments/write` at the target scope (pre
    Part-C deployments) the helper must NOT raise — record the failure
    into `roles_failed` and surface the exact `grant-runtime-rbac.sh`
    command so an admin can recover by hand. The cluster itself is still
    fully usable; failing the provision task here would be a worse UX.
    """

    class _AuthFailed(Exception):
        pass

    class FakeRoleAssignments:
        def create(
            self, *, scope: str, role_assignment_name: str, parameters: object
        ) -> None:
            raise _AuthFailed(
                "(AuthorizationFailed) The client 'mi-oid' does not have "
                "authorization to perform action "
                "'Microsoft.Authorization/roleAssignments/write' over scope "
                f"'{scope}'."
            )

    class FakeAuthorizationClient:
        def __init__(self, _cred: object, _sub: str) -> None:
            self.role_assignments = FakeRoleAssignments()

    import azure.mgmt.authorization as auth_mod

    monkeypatch.setattr(auth_mod, "AuthorizationManagementClient", FakeAuthorizationClient)

    result = azure._ensure_dashboard_mi_cluster_rg_roles(
        object(),
        subscription_id="sub-1",
        cluster_resource_group="rg-elb-cluster",
        mi_principal_id="mi-oid",
    )

    # Both attempts failed → both labels recorded; no exception raised.
    assert result["roles_assigned"] == []
    assert set(result["roles_failed"].keys()) == {
        "Contributor",
        "User Access Administrator",
    }
    for msg in result["roles_failed"].values():
        assert "AuthorizationFailed" in msg
    # Recovery command is always included so the SPA can render a copy
    # button even on the failure path.
    assert "grant-runtime-rbac.sh" in result["recovery_command"]


def test_ensure_dashboard_mi_cluster_rg_roles_treats_existing_assignment_as_success(
    monkeypatch,
) -> None:
    """Re-runs against an already-granted MI must idempotently land in
    `roles_assigned` (not `roles_failed`). The `_create_role_assignment_with_retry`
    helper turns `RoleAssignmentExists` into a silent success.
    """

    class _Conflict(Exception):
        pass

    class FakeRoleAssignments:
        def create(
            self, *, scope: str, role_assignment_name: str, parameters: object
        ) -> None:
            raise _Conflict(
                "(RoleAssignmentExists) The role assignment already exists."
            )

    class FakeAuthorizationClient:
        def __init__(self, _cred: object, _sub: str) -> None:
            self.role_assignments = FakeRoleAssignments()

    import azure.mgmt.authorization as auth_mod

    monkeypatch.setattr(auth_mod, "AuthorizationManagementClient", FakeAuthorizationClient)

    result = azure._ensure_dashboard_mi_cluster_rg_roles(
        object(),
        subscription_id="sub-1",
        cluster_resource_group="rg-elb-cluster",
        mi_principal_id="mi-oid",
    )

    assert result["roles_assigned"] == ["Contributor", "User Access Administrator"]
    assert result["roles_failed"] == {}


def test_ensure_dashboard_mi_cluster_rg_roles_falls_back_to_env_var(
    monkeypatch,
) -> None:
    """When `mi_principal_id=""`, the helper picks up
    `SHARED_IDENTITY_PRINCIPAL_ID` from the environment so callers in
    `provision_aks` do not have to pass it through explicitly.
    """
    captured: list[str] = []

    class FakeRoleAssignments:
        def create(
            self, *, scope: str, role_assignment_name: str, parameters: object
        ) -> None:
            captured.append(parameters.principal_id)

    class FakeAuthorizationClient:
        def __init__(self, _cred: object, _sub: str) -> None:
            self.role_assignments = FakeRoleAssignments()

    import azure.mgmt.authorization as auth_mod

    monkeypatch.setattr(auth_mod, "AuthorizationManagementClient", FakeAuthorizationClient)
    monkeypatch.setenv("SHARED_IDENTITY_PRINCIPAL_ID", "env-mi-oid")

    result = azure._ensure_dashboard_mi_cluster_rg_roles(
        object(),
        subscription_id="sub-1",
        cluster_resource_group="rg-elb-cluster",
        mi_principal_id="",
    )

    assert captured == ["env-mi-oid", "env-mi-oid"]
    assert result["mi_principal_id"] == "env-mi-oid"
    assert "--principal-id env-mi-oid" in result["recovery_command"]
