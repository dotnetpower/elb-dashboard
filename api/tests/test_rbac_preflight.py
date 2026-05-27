"""Tests for `api.services.rbac_preflight` and the rbac row in
`/api/aks/preflight`.

Responsibility: Verify the new MI RBAC preflight emits ok / fail / warn
    rows that match what the dashboard's Cluster Card renders, and that
    the row appears in the existing `/api/aks/preflight` response so the
    FE picks it up without any client-side change.
Edit boundaries: Pure unit tests with fake AuthorizationManagementClient
    and fake compute / resource clients; no live Azure.
Key entry points: see per-test docstrings.
Risky contracts: The `details.missing[]` shape is what
    `armErrorClassifier.ts` consumes for the "Copy az role assignment
    command" button — keep the field names stable.
Validation: `uv run pytest -q api/tests/test_rbac_preflight.py`.
"""

from __future__ import annotations

from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Mocks for AuthorizationManagementClient.
# ---------------------------------------------------------------------------


_OWNER_ID = "8e3af657-a8ff-443c-a75c-2fe8c4bcb635"
_CONTRIB_ID = "b24988ac-6180-42a0-ab88-20f7382dd24c"
_READER_ID = "acdd72a7-3385-48ef-bd42-f606fba81ae7"


def _role_id(sub: str, guid: str) -> str:
    """Build the full ARM role-definition id used inside role assignments."""
    return f"{sub}/providers/Microsoft.Authorization/roleDefinitions/{guid}"


class _RoleAssignment:
    def __init__(self, role_definition_id: str, scope: str) -> None:
        self.role_definition_id = role_definition_id
        self.scope = scope


class _RoleAssignmentsOp:
    def __init__(self, rows: list[_RoleAssignment]) -> None:
        self._rows = rows

    def list_for_subscription(self, filter: str | None = None) -> list[_RoleAssignment]:
        return list(self._rows)


class _RoleDefinitionsOp:
    def __init__(self, name_by_id: dict[str, str]) -> None:
        self._name_by_id = name_by_id

    def get_by_id(self, role_definition_id: str) -> Any:
        class _Def:
            role_name = self._name_by_id.get(role_definition_id.lower())

        return _Def()


class _FakeAuthClient:
    def __init__(
        self,
        assignments: list[_RoleAssignment],
        role_name_by_id: dict[str, str] | None = None,
    ) -> None:
        self.role_assignments = _RoleAssignmentsOp(assignments)
        self.role_definitions = _RoleDefinitionsOp(role_name_by_id or {})


def _patch_auth_client(monkeypatch: pytest.MonkeyPatch, client: _FakeAuthClient) -> None:
    """Patch AuthorizationManagementClient so both code paths in
    `_list_role_assignments` and `_resolve_role_name` receive `client`."""
    import azure.mgmt.authorization as auth_mod

    def _factory(_cred: Any, _sub: str) -> _FakeAuthClient:
        return client

    monkeypatch.setattr(auth_mod, "AuthorizationManagementClient", _factory)


# ---------------------------------------------------------------------------
# Service unit tests.
# ---------------------------------------------------------------------------


def test_rbac_check_ok_when_cluster_rg_contributor_and_sub_contributor_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sub-scope Contributor satisfies BOTH requirements at once (it
    covers managedClusters/write inside the cluster RG by inheritance
    AND resourceGroups/write at sub scope for the MC_* node RG)."""
    monkeypatch.setenv("SHARED_IDENTITY_PRINCIPAL_ID", "mi-oid")
    from api.services.rbac_preflight import aks_create_rbac_check

    sub = "/subscriptions/sub-1"
    rows = [
        _RoleAssignment(_role_id(sub, _CONTRIB_ID), sub),
    ]
    _patch_auth_client(monkeypatch, _FakeAuthClient(rows))

    check = aks_create_rbac_check(
        object(),
        subscription_id="sub-1",
        resource_group="rg-elb-cluster",
    )
    assert check.name == "rbac"
    assert check.status == "ok"


def test_rbac_check_ok_when_rg_contributor_and_custom_role_at_sub(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The narrow custom role at sub scope must satisfy the sub-scope
    requirement even when sub-scope Contributor is absent."""
    monkeypatch.setenv("SHARED_IDENTITY_PRINCIPAL_ID", "mi-oid")
    from api.services.rbac_preflight import aks_create_rbac_check

    sub = "/subscriptions/sub-1"
    rg_scope = f"{sub}/resourceGroups/rg-elb-cluster"
    custom_role_id = _role_id(sub, "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    rows = [
        _RoleAssignment(_role_id(sub, _CONTRIB_ID), rg_scope),
        _RoleAssignment(custom_role_id, sub),
    ]
    name_by_id = {custom_role_id.lower(): "Elb Workload RG Creator"}
    _patch_auth_client(monkeypatch, _FakeAuthClient(rows, name_by_id))

    check = aks_create_rbac_check(
        object(),
        subscription_id="sub-1",
        resource_group="rg-elb-cluster",
    )
    assert check.status == "ok"


def test_rbac_check_fail_when_sub_rg_write_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cluster RG Contributor alone is not enough — sub-scope RG-write
    must also be present (AKS MC_* auto-create requirement)."""
    monkeypatch.setenv("SHARED_IDENTITY_PRINCIPAL_ID", "mi-oid")
    from api.services.rbac_preflight import aks_create_rbac_check

    sub = "/subscriptions/sub-1"
    rg_scope = f"{sub}/resourceGroups/rg-elb-cluster"
    rows = [
        _RoleAssignment(_role_id(sub, _CONTRIB_ID), rg_scope),
        _RoleAssignment(_role_id(sub, _READER_ID), sub),
    ]
    _patch_auth_client(monkeypatch, _FakeAuthClient(rows))

    check = aks_create_rbac_check(
        object(),
        subscription_id="sub-1",
        resource_group="rg-elb-cluster",
    )
    assert check.status == "fail"
    missing = check.details["missing"]
    assert len(missing) == 1
    assert missing[0]["scope"] == "/subscriptions/sub-1"
    assert missing[0]["role"] == "Elb Workload RG Creator"
    assert "MC_<rg>" in missing[0]["reason"]
    # Custom-role remediation is "re-run azd up", not an az command.
    assert "az role assignment create" not in missing[0]["remediation"]


def test_rbac_check_ok_when_only_custom_role_at_sub_bootstraps_cluster_rg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Custom role at sub scope grants `roleAssignments/write` with an
    ABAC whitelist that includes Contributor + UAA (see
    `infra/modules/workloadRgCreatorRole.bicep`). The provision task
    self-grants those on the cluster RG before AKS create, so the
    cluster-RG Contributor requirement is satisfied as bootstrap-capable
    even when the per-RG assignment doesn't exist yet. This is the
    "user renamed the cluster → fresh `rg-<name>` doesn't have a
    pre-existing Contributor" path."""
    monkeypatch.setenv("SHARED_IDENTITY_PRINCIPAL_ID", "mi-oid")
    from api.services.rbac_preflight import aks_create_rbac_check

    sub = "/subscriptions/sub-1"
    custom_role_id = _role_id(sub, "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    rows = [
        _RoleAssignment(_role_id(sub, _READER_ID), sub),
        _RoleAssignment(custom_role_id, sub),
    ]
    name_by_id = {custom_role_id.lower(): "Elb Workload RG Creator"}
    _patch_auth_client(monkeypatch, _FakeAuthClient(rows, name_by_id))

    check = aks_create_rbac_check(
        object(),
        subscription_id="sub-1",
        resource_group="rg-elb-cluster-small",
    )
    assert check.status == "ok"
    assert "self-grant" in check.message.lower()
    assert "rg-elb-cluster-small" in check.message
    assert check.details["cluster_rg_bootstrap_capable"] is True


def test_rbac_check_fail_when_no_sub_scope_grants_at_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without sub-scope Contributor AND without the custom role, the MI
    has neither the cluster-RG grant nor the bootstrap capability —
    preflight must fail with both gaps surfaced."""
    monkeypatch.setenv("SHARED_IDENTITY_PRINCIPAL_ID", "mi-oid")
    from api.services.rbac_preflight import aks_create_rbac_check

    sub = "/subscriptions/sub-1"
    rows = [
        _RoleAssignment(_role_id(sub, _READER_ID), sub),
    ]
    _patch_auth_client(monkeypatch, _FakeAuthClient(rows))

    check = aks_create_rbac_check(
        object(),
        subscription_id="sub-1",
        resource_group="rg-elb-cluster",
    )
    assert check.status == "fail"
    missing = check.details["missing"]
    # Both the cluster RG Contributor and the sub-scope custom role
    # are missing — bootstrap path is unavailable.
    roles = {m["role"] for m in missing}
    assert "Contributor" in roles
    assert "Elb Workload RG Creator" in roles


def test_rbac_check_warn_when_principal_id_env_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local-dev shell without SHARED_IDENTITY_PRINCIPAL_ID — degrade to
    warn (never block submit on a missing env value)."""
    monkeypatch.delenv("SHARED_IDENTITY_PRINCIPAL_ID", raising=False)
    from api.services.rbac_preflight import aks_create_rbac_check

    check = aks_create_rbac_check(
        object(),
        subscription_id="sub-1",
        resource_group="rg-elb-cluster",
    )
    assert check.status == "warn"
    assert "SHARED_IDENTITY_PRINCIPAL_ID" in check.message


def test_rbac_check_warn_when_role_enumeration_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the caller lacks Microsoft.Authorization/roleAssignments/read
    we cannot enumerate — preflight degrades to warn so ARM stays the
    ground truth (no false-negative submit blocks)."""
    monkeypatch.setenv("SHARED_IDENTITY_PRINCIPAL_ID", "mi-oid")
    import azure.mgmt.authorization as auth_mod

    class _BoomClient:
        @property
        def role_assignments(self) -> Any:
            raise RuntimeError("Forbidden: roleAssignments/read missing")

    monkeypatch.setattr(auth_mod, "AuthorizationManagementClient", lambda _c, _s: _BoomClient())
    from api.services.rbac_preflight import aks_create_rbac_check

    check = aks_create_rbac_check(
        object(),
        subscription_id="sub-1",
        resource_group="rg-elb-cluster",
    )
    assert check.status == "warn"
    assert "Cannot list role assignments" in check.message


# ---------------------------------------------------------------------------
# `aks_runtime_rbac_check` — UAA detection for the ensuring_rbac step.
# ---------------------------------------------------------------------------

_UAA_ID = "18d7d88d-d35e-4fb5-a5c3-7773c20a72d9"


def test_runtime_rbac_ok_when_uaa_at_sub_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """User Access Administrator at subscription scope covers every
    runtime RBAC target (ACR + Storage) by inheritance."""
    monkeypatch.setenv("SHARED_IDENTITY_PRINCIPAL_ID", "mi-oid")
    from api.services.rbac_preflight import aks_runtime_rbac_check

    sub = "/subscriptions/sub-1"
    rows = [_RoleAssignment(_role_id(sub, _UAA_ID), sub)]
    _patch_auth_client(monkeypatch, _FakeAuthClient(rows))

    check = aks_runtime_rbac_check(
        object(),
        subscription_id="sub-1",
        resource_group="rg-elb",
        acr_resource_group="rg-acr",
        acr_name="elbacr",
        storage_resource_group="rg-stg",
        storage_account="elbstg",
    )
    assert check.status == "ok"


def test_runtime_rbac_ok_when_uaa_at_target_rg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UAA at the target's containing RG must cover the target — and the
    match must NOT false-positive on a sibling RG with a prefix-overlapping
    name (e.g. `rg` vs `rg-acr`)."""
    monkeypatch.setenv("SHARED_IDENTITY_PRINCIPAL_ID", "mi-oid")
    from api.services.rbac_preflight import aks_runtime_rbac_check

    sub = "/subscriptions/sub-1"
    rg_acr_scope = f"{sub}/resourceGroups/rg-acr"
    rg_stg_scope = f"{sub}/resourceGroups/rg-stg"
    rows = [
        _RoleAssignment(_role_id(sub, _UAA_ID), rg_acr_scope),
        _RoleAssignment(_role_id(sub, _UAA_ID), rg_stg_scope),
    ]
    _patch_auth_client(monkeypatch, _FakeAuthClient(rows))

    check = aks_runtime_rbac_check(
        object(),
        subscription_id="sub-1",
        resource_group="rg-elb",
        acr_resource_group="rg-acr",
        acr_name="elbacr",
        storage_resource_group="rg-stg",
        storage_account="elbstg",
    )
    assert check.status == "ok"


def test_runtime_rbac_prefix_match_does_not_false_positive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: `target.startswith(grant)` was matching across RGs
    whose names share a prefix. A UAA grant on `/...resourceGroups/rg`
    must NOT cover a target inside `/...resourceGroups/rg-acr/...`."""
    monkeypatch.setenv("SHARED_IDENTITY_PRINCIPAL_ID", "mi-oid")
    from api.services.rbac_preflight import aks_runtime_rbac_check

    sub = "/subscriptions/sub-1"
    # UAA only at /resourceGroups/rg — must NOT cover /resourceGroups/rg-acr
    rows = [_RoleAssignment(_role_id(sub, _UAA_ID), f"{sub}/resourceGroups/rg")]
    _patch_auth_client(monkeypatch, _FakeAuthClient(rows))

    check = aks_runtime_rbac_check(
        object(),
        subscription_id="sub-1",
        resource_group="rg-elb",
        acr_resource_group="rg-acr",
        acr_name="elbacr",
    )
    assert check.status == "warn", check.details
    missing = check.details["missing"]
    assert len(missing) == 1
    assert "AcrPull" in missing[0]["role_assignment"]


def test_runtime_rbac_warn_when_no_uaa(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sub-scope Contributor alone does NOT cover the role-assignment
    write — must report warn with both targets missing."""
    monkeypatch.setenv("SHARED_IDENTITY_PRINCIPAL_ID", "mi-oid")
    from api.services.rbac_preflight import aks_runtime_rbac_check

    sub = "/subscriptions/sub-1"
    rows = [_RoleAssignment(_role_id(sub, _CONTRIB_ID), sub)]
    _patch_auth_client(monkeypatch, _FakeAuthClient(rows))

    check = aks_runtime_rbac_check(
        object(),
        subscription_id="sub-1",
        resource_group="rg-elb",
        acr_resource_group="rg-acr",
        acr_name="elbacr",
        storage_resource_group="rg-stg",
        storage_account="elbstg",
    )
    assert check.status == "warn"
    assert len(check.details["missing"]) == 2
    # Remediation must be a copy-pasteable az command.
    for row in check.details["missing"]:
        assert "az role assignment create" in row["remediation"]
        assert "User Access Administrator" in row["remediation"]


def test_runtime_rbac_ok_when_no_targets_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ACR / Storage targets means the provision task's RBAC step is a
    no-op — report ok rather than warn so the checklist stays clean."""
    monkeypatch.setenv("SHARED_IDENTITY_PRINCIPAL_ID", "mi-oid")
    from api.services.rbac_preflight import aks_runtime_rbac_check

    check = aks_runtime_rbac_check(
        object(),
        subscription_id="sub-1",
        resource_group="rg-elb",
    )
    assert check.status == "ok"
    assert check.details["targets"] == []


def test_runtime_rbac_warn_when_principal_id_env_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mirrors aks_create_rbac_check: degrade to warn when we cannot
    identify the MI principal."""
    monkeypatch.delenv("SHARED_IDENTITY_PRINCIPAL_ID", raising=False)
    from api.services.rbac_preflight import aks_runtime_rbac_check

    check = aks_runtime_rbac_check(
        object(),
        subscription_id="sub-1",
        resource_group="rg-elb",
        acr_resource_group="rg-acr",
        acr_name="elbacr",
    )
    assert check.status == "warn"
    assert "SHARED_IDENTITY_PRINCIPAL_ID" in check.message


# ---------------------------------------------------------------------------
# Route integration: rbac row appears in /api/aks/preflight.
# ---------------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Any:
    from fastapi.testclient import TestClient

    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setenv("API_CLIENT_ID", "00000000-0000-0000-0000-000000000000")
    from api.main import app

    return TestClient(app)


def test_route_preflight_includes_rbac_row(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The existing preflight route must surface the new rbac row in
    `checks[]` so the FE picks it up without an API contract change."""
    import api.routes.aks.preflight as preflight_mod
    import api.services.aks_availability as availability

    # Minimal SKU + RG stubs so the SKU / quota / RG rows pass.
    class _Sku:
        def __init__(self, name: str) -> None:
            self.name = name
            self.resource_type = "virtualMachines"
            self.locations = ["koreacentral"]
            self.restrictions = []

    class _Usage:
        def __init__(self, name: str, current: int, limit: int) -> None:
            class _Nm:
                value = name

            self.name = _Nm()
            self.current_value = current
            self.limit = limit

    class _FakeCompute:
        def __init__(self) -> None:
            class _Skus:
                def list(self, filter: str | None = None) -> list[_Sku]:
                    return [_Sku("Standard_E16s_v5"), _Sku("Standard_D2s_v3")]

            class _Usages:
                def list(self, _r: str) -> list[_Usage]:
                    return [
                        _Usage("standardESv5Family", 0, 200),
                        _Usage("standardDSv3Family", 0, 200),
                        _Usage("cores", 0, 400),
                    ]

            self.resource_skus = _Skus()
            self.usage = _Usages()

    class _FakeRc:
        class resource_groups:
            @staticmethod
            def get(_n: str) -> Any:
                class _Rg:
                    location = "koreacentral"

                return _Rg()

    monkeypatch.setattr(preflight_mod, "get_credential", lambda: object())
    fake_compute = _FakeCompute()
    monkeypatch.setattr(availability, "compute_client", lambda _c, _s: fake_compute)
    monkeypatch.setattr(availability, "resource_client", lambda _c, _s: _FakeRc())

    # Configure RBAC mock — sub-scope Contributor (passes both checks).
    sub = "/subscriptions/sub-1"
    rows = [
        _RoleAssignment(_role_id(sub, _CONTRIB_ID), sub),
    ]
    monkeypatch.setenv("SHARED_IDENTITY_PRINCIPAL_ID", "mi-oid")
    _patch_auth_client(monkeypatch, _FakeAuthClient(rows))

    resp = client.post(
        "/api/aks/preflight",
        json={
            "subscription_id": "sub-1",
            "resource_group": "rg-elb-cluster",
            "region": "koreacentral",
            "cluster_name": "elb-cluster-01",
            "node_sku": "Standard_E16s_v5",
            "node_count": 1,
            "system_vm_size": "Standard_D2s_v3",
            "system_node_count": 1,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    by_name = {c["name"]: c for c in body["checks"]}
    assert "rbac" in by_name, body["checks"]
    assert by_name["rbac"]["status"] == "ok"
