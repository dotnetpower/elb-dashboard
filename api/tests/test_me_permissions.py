"""Tests for caller RBAC permission computation.

Responsibility: Pin the role-GUID \u2192 capability mapping and the
    ancestor-scope inclusion logic so a future tweak (e.g. adding a
    new built-in role to the read set) is caught in CI before SPA
    callers regress.
Edit boundaries: Service-layer unit tests. Route-level tests live in
    `test_me_route.py`. No real ARM calls \u2014 the
    `AuthorizationManagementClient` is monkeypatched.
Key entry points: `compute_caller_permissions`.
Risky contracts: Degrade-open behaviour on enumeration failure must
    keep all capabilities ``True`` and ``degraded=True`` so the SPA
    does not lock the operator out on a transient ARM hiccup.
Validation: `uv run pytest -q api/tests/test_me_permissions.py`.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from api.services import me_permissions
from api.services.me_permissions import compute_caller_permissions


@dataclass
class _FakeAssignment:
    role_definition_id: str
    scope: str


class _FakeRoleAssignmentsAPI:
    def __init__(self, rows: list[_FakeAssignment]) -> None:
        self._rows = rows
        self.calls: list[tuple[str | None, str | None]] = []

    def list_for_subscription(self, filter: str | None = None):
        self.calls.append(("list_for_subscription", filter))
        return iter(self._rows)


class _FakeAuthClient:
    def __init__(self, rows: list[_FakeAssignment]) -> None:
        self.role_assignments = _FakeRoleAssignmentsAPI(rows)


def _patch_client(monkeypatch: pytest.MonkeyPatch, rows: list[_FakeAssignment]) -> _FakeAuthClient:
    fake = _FakeAuthClient(rows)

    class _FakeModule:
        @staticmethod
        def AuthorizationManagementClient(credential, subscription_id):
            return fake

    monkeypatch.setitem(__import__("sys").modules, "azure.mgmt.authorization", _FakeModule)
    return fake


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    me_permissions.reset_permissions_cache_for_tests()
    yield
    me_permissions.reset_permissions_cache_for_tests()


def _row(role_guid: str, scope: str) -> _FakeAssignment:
    return _FakeAssignment(
        role_definition_id=(
            f"/subscriptions/SUB/providers/Microsoft.Authorization/roleDefinitions/{role_guid}"
        ),
        scope=scope,
    )


def test_owner_at_sub_scope_grants_every_capability(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_client(
        monkeypatch,
        [_row("8e3af657-a8ff-443c-a75c-2fe8c4bcb635", "/subscriptions/SUB")],
    )

    perms = compute_caller_permissions(
        object(),
        caller_oid="11111111-2222-3333-4444-555555555555",
        subscription_id="SUB",
        resource_group="rg-elb",
        cluster_name="elb-cluster",
    )

    assert perms.can_read is True
    assert perms.can_write is True
    assert perms.can_start_stop is True
    assert perms.can_delete is True
    assert perms.can_submit_blast is True
    assert perms.can_build_acr is True
    assert perms.can_grant_rbac is True
    assert perms.degraded is False
    assert "Owner" in perms.matched_role_names


def test_reader_at_rg_scope_grants_read_but_blocks_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Reader role GUID = acdd72a7-3385-48ef-bd42-f606fba81ae7
    _patch_client(
        monkeypatch,
        [_row("acdd72a7-3385-48ef-bd42-f606fba81ae7", "/subscriptions/SUB/resourceGroups/rg-elb")],
    )

    perms = compute_caller_permissions(
        object(),
        caller_oid="11111111-2222-3333-4444-555555555555",
        subscription_id="SUB",
        resource_group="rg-elb",
    )

    assert perms.can_read is True
    assert perms.can_write is False
    assert perms.can_start_stop is False
    assert perms.can_delete is False
    assert perms.can_submit_blast is False
    assert perms.can_build_acr is False
    assert perms.can_grant_rbac is False
    assert perms.degraded is False
    assert "Reader" in perms.matched_role_names


def test_enumeration_uses_assigned_to_filter_for_group_transitivity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The OData filter MUST be ``assignedTo('<oid>')`` and NOT
    ``principalId eq '<oid>'``.

    ``principalId eq`` only matches assignments whose principal is the
    caller's own object id, so a user who holds a role purely through
    Entra group membership gets zero rows and is wrongly treated as
    having no access. ``assignedTo()`` is the Azure-CLI ``--include-groups``
    filter that expands transitive group membership. This test pins the
    filter string so a future refactor cannot silently regress to the
    direct-only form (which broke group-granted Reader/Contributor)."""
    fake = _patch_client(
        monkeypatch,
        [_row("acdd72a7-3385-48ef-bd42-f606fba81ae7", "/subscriptions/SUB")],
    )

    compute_caller_permissions(
        object(),
        caller_oid="11111111-2222-3333-4444-555555555555",
        subscription_id="SUB",
        resource_group="rg-elb",
    )

    assert fake.role_assignments.calls, "enumeration did not call ARM"
    sent_filter = fake.role_assignments.calls[0][1] or ""
    assert sent_filter == "assignedTo('11111111-2222-3333-4444-555555555555')"
    assert "principalId eq" not in sent_filter


def test_group_inherited_reader_is_recognized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end behaviour of the group fix: when ARM returns (via
    ``assignedTo``) a Reader assignment the caller holds through a group
    at the workload RG scope, the resolver must recognise read access
    instead of falling through to ``no_role_at_scope``."""
    _patch_client(
        monkeypatch,
        [_row("acdd72a7-3385-48ef-bd42-f606fba81ae7", "/subscriptions/SUB/resourceGroups/rg-elb")],
    )

    perms = compute_caller_permissions(
        object(),
        caller_oid="11111111-2222-3333-4444-555555555555",
        subscription_id="SUB",
        resource_group="rg-elb",
    )

    assert perms.can_read is True
    assert perms.degraded is False
    assert "Reader" in perms.matched_role_names
    assert perms.reason == ""


def test_owner_inherited_from_management_group_grants_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A subscription Owner who holds Owner at a MANAGEMENT-GROUP scope
    (the common pattern for the tenant's global admins) must be
    recognised at a cluster scope inside the subscription.

    ``list_for_subscription`` + ``assignedTo`` returns the inherited MG
    assignment, but its scope
    (``/providers/Microsoft.Management/managementGroups/<id>``) is not a
    string prefix of the ``/subscriptions/<sub>/...`` target, so the old
    prefix-only ancestor check dropped it and the operator was wrongly
    told they hold ``no Azure RBAC role at this scope``. This pins the
    fix so the regression cannot return."""
    _patch_client(
        monkeypatch,
        [
            _row(
                "8e3af657-a8ff-443c-a75c-2fe8c4bcb635",
                "/providers/Microsoft.Management/managementGroups/00000000-1111-2222-3333-444444444444",
            )
        ],
    )

    perms = compute_caller_permissions(
        object(),
        caller_oid="11111111-2222-3333-4444-555555555555",
        subscription_id="SUB",
        resource_group="rg-elb",
        cluster_name="aks-elb",
    )

    assert perms.can_delete is True
    assert perms.can_read is True
    assert perms.degraded is False
    assert "Owner" in perms.matched_role_names
    assert perms.reason == ""


def test_owner_inherited_from_tenant_root_grants_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same as the management-group case but for an Owner assignment at
    the tenant ROOT scope (``/``). ``rstrip('/')`` collapses the root
    scope to an empty string, which the old guard treated as a malformed
    row and rejected — wrongly hiding Delete from a tenant-root Owner."""
    _patch_client(
        monkeypatch,
        [_row("8e3af657-a8ff-443c-a75c-2fe8c4bcb635", "/")],
    )

    perms = compute_caller_permissions(
        object(),
        caller_oid="11111111-2222-3333-4444-555555555555",
        subscription_id="SUB",
        resource_group="rg-elb",
        cluster_name="aks-elb",
    )

    assert perms.can_delete is True
    assert perms.can_read is True
    assert perms.degraded is False
    assert "Owner" in perms.matched_role_names
    assert perms.reason == ""


def test_contributor_grants_writes_but_not_delete_or_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Contributor = b24988ac-6180-42a0-ab88-20f7382dd24c
    _patch_client(
        monkeypatch,
        [_row("b24988ac-6180-42a0-ab88-20f7382dd24c", "/subscriptions/SUB")],
    )

    perms = compute_caller_permissions(
        object(),
        caller_oid="11111111-2222-3333-4444-555555555555",
        subscription_id="SUB",
        resource_group="rg-elb",
    )

    assert perms.can_read is True
    assert perms.can_write is True
    assert perms.can_start_stop is True
    assert perms.can_delete is False
    assert perms.can_submit_blast is True
    assert perms.can_build_acr is True
    assert perms.can_grant_rbac is False


def test_uaa_grants_rbac_but_not_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    # User Access Administrator = 18d7d88d-d35e-4fb5-a5c3-7773c20a72d9
    _patch_client(
        monkeypatch,
        [_row("18d7d88d-d35e-4fb5-a5c3-7773c20a72d9", "/subscriptions/SUB")],
    )

    perms = compute_caller_permissions(
        object(),
        caller_oid="11111111-2222-3333-4444-555555555555",
        subscription_id="SUB",
    )

    assert perms.can_grant_rbac is True
    assert perms.can_write is False


def test_assignment_at_cluster_scope_is_visible_at_cluster_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A role assigned at the leaf resource must satisfy the same-scope query."""
    cluster_scope = (
        "/subscriptions/SUB/resourceGroups/rg-elb/providers/"
        "Microsoft.ContainerService/managedClusters/elb-cluster"
    )
    _patch_client(
        monkeypatch,
        [_row("ed7f3fbd-7b88-4dd4-9017-9adb7ce333f8", cluster_scope)],
    )

    perms = compute_caller_permissions(
        object(),
        caller_oid="11111111-2222-3333-4444-555555555555",
        subscription_id="SUB",
        resource_group="rg-elb",
        cluster_name="elb-cluster",
    )
    # Azure Kubernetes Service Contributor Role
    assert perms.can_start_stop is True


def test_descendant_scope_assignment_does_not_grant_parent_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A role assigned at a CHILD scope must NOT satisfy a query at the
    PARENT scope. (Reverse of the inheritance direction.)"""
    cluster_scope = (
        "/subscriptions/SUB/resourceGroups/rg-elb/providers/"
        "Microsoft.ContainerService/managedClusters/elb-cluster"
    )
    _patch_client(
        monkeypatch,
        [_row("8e3af657-a8ff-443c-a75c-2fe8c4bcb635", cluster_scope)],
    )

    # Query at the SUBSCRIPTION scope \u2014 cluster-scoped Owner must
    # NOT bubble up.
    perms = compute_caller_permissions(
        object(),
        caller_oid="11111111-2222-3333-4444-555555555555",
        subscription_id="SUB",
    )
    assert perms.can_write is False
    assert perms.reason == "no_role_at_scope"


def test_enumeration_failure_degrades_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """Critique #6: a transient ARM enumeration failure must NOT lock
    the operator out of legitimate actions. Real authorization runs at
    submit time inside ARM."""

    class _FakeAuthClientThatRaises:
        @property
        def role_assignments(self):
            raise RuntimeError("ARM unavailable")

    class _FakeModule:
        @staticmethod
        def AuthorizationManagementClient(credential, subscription_id):
            return _FakeAuthClientThatRaises()

    monkeypatch.setitem(
        __import__("sys").modules, "azure.mgmt.authorization", _FakeModule
    )

    perms = compute_caller_permissions(
        object(),
        caller_oid="11111111-2222-3333-4444-555555555555",
        subscription_id="SUB",
    )

    assert perms.degraded is True
    assert perms.can_read is True
    assert perms.can_write is True
    assert perms.can_start_stop is True
    assert perms.can_delete is True
    assert perms.can_submit_blast is True
    assert perms.can_build_acr is True
    assert perms.can_grant_rbac is True


def test_empty_caller_oid_returns_no_permissions(monkeypatch: pytest.MonkeyPatch) -> None:
    perms = compute_caller_permissions(
        object(),
        caller_oid="",
        subscription_id="SUB",
    )
    assert perms.can_read is False
    assert perms.can_write is False
    assert perms.degraded is False
    assert perms.reason == "no_caller_oid"


def test_caller_permissions_are_cached_per_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ARM enumeration is rate-limited; second poll within TTL must
    hit the cache instead of re-listing."""
    fake = _patch_client(
        monkeypatch,
        [_row("8e3af657-a8ff-443c-a75c-2fe8c4bcb635", "/subscriptions/SUB")],
    )

    for _ in range(5):
        compute_caller_permissions(
            object(),
            caller_oid="11111111-2222-3333-4444-555555555555",
            subscription_id="SUB",
            resource_group="rg-elb",
        )

    # Exactly one enumeration call despite 5 queries.
    assert len(fake.role_assignments.calls) == 1


def test_different_scopes_get_independent_cache_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Different (caller, scope) pairs must NOT share a cache entry."""
    fake = _patch_client(
        monkeypatch,
        [_row("8e3af657-a8ff-443c-a75c-2fe8c4bcb635", "/subscriptions/SUB")],
    )

    _OID = "11111111-2222-3333-4444-555555555555"
    compute_caller_permissions(object(), caller_oid=_OID, subscription_id="SUB")
    compute_caller_permissions(
        object(), caller_oid=_OID, subscription_id="SUB", resource_group="rg-A"
    )
    compute_caller_permissions(
        object(), caller_oid=_OID, subscription_id="SUB", resource_group="rg-B"
    )

    assert len(fake.role_assignments.calls) == 3


def test_invalid_oid_format_degrades_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """Critique-round-1 C5: ``caller_oid`` is interpolated into an OData
    ``filter`` string, so a non-UUID value (which would never come from
    a validated JWT) must be rejected up-front to prevent OData clause
    injection. Rejection takes the same degrade-open path as a
    transient ARM failure so a defensive false-positive never locks
    the operator out.
    """
    fake = _patch_client(
        monkeypatch,
        [_row("8e3af657-a8ff-443c-a75c-2fe8c4bcb635", "/subscriptions/SUB")],
    )

    perms = compute_caller_permissions(
        object(),
        caller_oid="user-oid' or 1=1--",  # OData injection attempt
        subscription_id="SUB",
    )

    # The injection attempt never reaches ARM \u2014 ``fake`` records
    # zero calls.
    assert len(fake.role_assignments.calls) == 0
    # Degrade-open: all capabilities true so the UX does not break,
    # but ``degraded=true`` so the SPA can render the diagnostic
    # banner.
    assert perms.degraded is True
    assert perms.reason == "invalid_oid_format"
    assert perms.can_write is True  # degrade-open
    # No real matched roles because we never enumerated.
    assert perms.matched_roles == ()
