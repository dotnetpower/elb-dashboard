"""Tests for per-resource-group RBAC access review.

Responsibility: Pin the access-review grouping, inheritance flagging,
    scope-level classification, role-name resolution, and the
    no-degrade-open contract so a future tweak does not silently turn the
    diagnostic into a fabricated "you have access".
Edit boundaries: Service-layer unit tests. Route-level wiring is covered
    in `test_me_route.py`. No real ARM calls — the
    `AuthorizationManagementClient` is monkeypatched.
Key entry points: `review_resource_group_access`.
Risky contracts: Enumeration failure must mark every group
    ``degraded=True`` (NOT open). Group-inherited assignments
    (assignedTo semantics) and management-group inheritance must surface
    as ``inherited=True``.
Validation: `uv run pytest -q api/tests/test_access_review.py`.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from api.services import access_review
from api.services.access_review import review_resource_group_access

_OWNER = "8e3af657-a8ff-443c-a75c-2fe8c4bcb635"
_READER = "acdd72a7-3385-48ef-bd42-f606fba81ae7"
_CONTRIB = "b24988ac-6180-42a0-ab88-20f7382dd24c"
_CUSTOM = "deadbeef-0000-0000-0000-000000000001"
_OID = "11111111-2222-3333-4444-555555555555"


@dataclass
class _FakeAssignment:
    role_definition_id: str
    scope: str


@dataclass
class _FakeRoleDef:
    role_name: str


class _FakeRoleAssignmentsAPI:
    def __init__(self, rows: list[_FakeAssignment], raise_on_list: bool) -> None:
        self._rows = rows
        self._raise = raise_on_list
        self.calls: list[str | None] = []

    def list_for_subscription(self, filter: str | None = None):
        self.calls.append(filter)
        if self._raise:
            raise RuntimeError("AuthorizationFailed: roleAssignments/read denied")
        return iter(self._rows)


class _FakeRoleDefinitionsAPI:
    def __init__(self, names: dict[str, str]) -> None:
        self._names = names
        self.gets: list[str] = []

    def get_by_id(self, role_definition_id: str):
        self.gets.append(role_definition_id)
        guid = role_definition_id.rsplit("/", 1)[-1]
        return _FakeRoleDef(role_name=self._names.get(guid, "Unknown"))


class _FakeAuthClient:
    def __init__(
        self,
        rows: list[_FakeAssignment],
        names: dict[str, str],
        raise_on_list: bool,
    ) -> None:
        self.role_assignments = _FakeRoleAssignmentsAPI(rows, raise_on_list)
        self.role_definitions = _FakeRoleDefinitionsAPI(names)


def _patch_client(
    monkeypatch: pytest.MonkeyPatch,
    rows: list[_FakeAssignment],
    *,
    names: dict[str, str] | None = None,
    raise_on_list: bool = False,
) -> _FakeAuthClient:
    fake = _FakeAuthClient(rows, names or {}, raise_on_list)

    class _FakeModule:
        @staticmethod
        def AuthorizationManagementClient(credential, subscription_id):
            return fake

    monkeypatch.setitem(__import__("sys").modules, "azure.mgmt.authorization", _FakeModule)
    return fake


@pytest.fixture(autouse=True)
def _clear_cache():
    access_review.reset_access_review_cache_for_tests()
    yield
    access_review.reset_access_review_cache_for_tests()


def _row(role_guid: str, scope: str) -> _FakeAssignment:
    return _FakeAssignment(
        role_definition_id=(
            "/subscriptions/SUB/providers/Microsoft.Authorization/"
            f"roleDefinitions/{role_guid}"
        ),
        scope=scope,
    )


def test_direct_rg_assignment_is_not_inherited(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_client(
        monkeypatch,
        [_row(_CONTRIB, "/subscriptions/SUB/resourceGroups/rg-elb")],
    )

    review = review_resource_group_access(
        object(),
        principal_oid=_OID,
        subscription_id="SUB",
        resource_groups=["rg-elb"],
    )

    assert review.subscription_id == "SUB"
    assert len(review.groups) == 1
    grp = review.groups[0]
    assert grp.resource_group == "rg-elb"
    assert grp.degraded is False
    assert len(grp.assignments) == 1
    row = grp.assignments[0]
    assert row.role_name == "Contributor"
    assert row.inherited is False
    assert row.scope_level == "resource_group"


def test_subscription_assignment_is_inherited(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_client(monkeypatch, [_row(_OWNER, "/subscriptions/SUB")])

    review = review_resource_group_access(
        object(),
        principal_oid=_OID,
        subscription_id="SUB",
        resource_groups=["rg-elb"],
    )

    row = review.groups[0].assignments[0]
    assert row.role_name == "Owner"
    assert row.inherited is True
    assert row.scope_level == "subscription"


def test_management_group_assignment_inherited(monkeypatch: pytest.MonkeyPatch) -> None:
    mg_scope = "/providers/Microsoft.Management/managementGroups/mg-root"
    _patch_client(monkeypatch, [_row(_READER, mg_scope)])

    review = review_resource_group_access(
        object(),
        principal_oid=_OID,
        subscription_id="SUB",
        resource_groups=["rg-elb"],
    )

    row = review.groups[0].assignments[0]
    assert row.role_name == "Reader"
    assert row.inherited is True
    assert row.scope_level == "management_group"


def test_tenant_root_assignment_inherited(monkeypatch: pytest.MonkeyPatch) -> None:
    """An Owner assignment at the tenant ROOT scope (``/``) must surface as
    an inherited row. ``rstrip('/')`` collapses the root scope to an empty
    string, which the old guard treated as a malformed row and dropped —
    hiding tenant-root Owners from the access-review panel."""
    _patch_client(monkeypatch, [_row(_OWNER, "/")])

    review = review_resource_group_access(
        object(),
        principal_oid=_OID,
        subscription_id="SUB",
        resource_groups=["rg-elb"],
    )

    grp = review.groups[0]
    assert grp.degraded is False
    assert len(grp.assignments) == 1
    row = grp.assignments[0]
    assert row.role_name == "Owner"
    assert row.inherited is True


def test_unrelated_rg_assignment_does_not_match(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_client(
        monkeypatch,
        [_row(_CONTRIB, "/subscriptions/SUB/resourceGroups/rg-other")],
    )

    review = review_resource_group_access(
        object(),
        principal_oid=_OID,
        subscription_id="SUB",
        resource_groups=["rg-elb"],
    )

    grp = review.groups[0]
    assert grp.assignments == ()
    assert grp.degraded is False
    assert grp.reason == "no_role_at_scope"


def test_multiple_resource_groups_grouped_independently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_client(
        monkeypatch,
        [
            _row(_OWNER, "/subscriptions/SUB"),
            _row(_CONTRIB, "/subscriptions/SUB/resourceGroups/rg-cluster"),
        ],
    )

    review = review_resource_group_access(
        object(),
        principal_oid=_OID,
        subscription_id="SUB",
        resource_groups=["rg-dashboard", "rg-cluster"],
    )

    by_rg = {g.resource_group: g for g in review.groups}
    # rg-dashboard only inherits the sub-scope Owner.
    assert [a.role_name for a in by_rg["rg-dashboard"].assignments] == ["Owner"]
    # rg-cluster has both the inherited Owner and its direct Contributor.
    names = sorted(a.role_name for a in by_rg["rg-cluster"].assignments)
    assert names == ["Contributor", "Owner"]


def test_enumeration_failure_degrades_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_client(monkeypatch, [], raise_on_list=True)

    review = review_resource_group_access(
        object(),
        principal_oid=_OID,
        subscription_id="SUB",
        resource_groups=["rg-elb"],
    )

    grp = review.groups[0]
    assert grp.degraded is True
    assert grp.assignments == ()
    assert "AuthorizationFailed" in grp.reason or "RuntimeError" in grp.reason


def test_invalid_oid_degrades_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_client(monkeypatch, [_row(_OWNER, "/subscriptions/SUB")])

    review = review_resource_group_access(
        object(),
        principal_oid="not-a-uuid",
        subscription_id="SUB",
        resource_groups=["rg-elb"],
    )

    grp = review.groups[0]
    assert grp.degraded is True
    assert grp.reason == "invalid_oid_format"


def test_invalid_rg_name_degrades_only_that_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_client(monkeypatch, [_row(_OWNER, "/subscriptions/SUB")])

    review = review_resource_group_access(
        object(),
        principal_oid=_OID,
        subscription_id="SUB",
        resource_groups=["rg-good", "bad/name"],
    )

    by_rg = {g.resource_group: g for g in review.groups}
    assert by_rg["rg-good"].degraded is False
    assert by_rg["bad/name"].degraded is True
    assert by_rg["bad/name"].reason == "invalid_resource_group_name"


def test_custom_role_resolved_via_get_by_id(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _patch_client(
        monkeypatch,
        [_row(_CUSTOM, "/subscriptions/SUB/resourceGroups/rg-elb")],
        names={_CUSTOM: "Elb Workload RG Creator"},
    )

    review = review_resource_group_access(
        object(),
        principal_oid=_OID,
        subscription_id="SUB",
        resource_groups=["rg-elb"],
    )

    row = review.groups[0].assignments[0]
    assert row.role_name == "Elb Workload RG Creator"
    assert fake.role_definitions.gets  # the custom role triggered a lookup


def test_duplicate_resource_groups_deduped(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_client(monkeypatch, [_row(_OWNER, "/subscriptions/SUB")])

    review = review_resource_group_access(
        object(),
        principal_oid=_OID,
        subscription_id="SUB",
        resource_groups=["rg-elb", "RG-ELB", "  ", ""],
    )

    assert len(review.groups) == 1
    assert review.groups[0].resource_group == "rg-elb"


def test_empty_inputs_return_empty_review(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_client(monkeypatch, [_row(_OWNER, "/subscriptions/SUB")])

    assert review_resource_group_access(
        object(), principal_oid=_OID, subscription_id="", resource_groups=["rg"]
    ).groups == ()
    assert review_resource_group_access(
        object(), principal_oid=_OID, subscription_id="SUB", resource_groups=[]
    ).groups == ()


def test_principal_metadata_user(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_client(monkeypatch, [_row(_OWNER, "/subscriptions/SUB")])

    review = review_resource_group_access(
        object(),
        principal_oid=_OID,
        subscription_id="SUB",
        resource_groups=["rg-elb"],
    )

    assert review.principal.kind == "user"
    assert review.principal.object_id == _OID
    assert review.principal.available is True


def test_dashboard_identity_kind_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_client(monkeypatch, [_row(_OWNER, "/subscriptions/SUB")])

    review = review_resource_group_access(
        object(),
        principal_oid=_OID,
        subscription_id="SUB",
        resource_groups=["rg-elb"],
        principal_kind="dashboard_identity",
    )

    assert review.principal.kind == "dashboard_identity"
    assert review.principal.available is True
    assert review.groups[0].assignments[0].role_name == "Owner"


def test_empty_principal_oid_marks_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_client(monkeypatch, [_row(_OWNER, "/subscriptions/SUB")])

    review = review_resource_group_access(
        object(),
        principal_oid="",
        subscription_id="SUB",
        resource_groups=["rg-elb"],
        principal_kind="dashboard_identity",
    )

    assert review.principal.kind == "dashboard_identity"
    assert review.principal.available is False
    assert review.groups == ()


def test_dashboard_identity_principal_id_reads_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api.services.access_review import dashboard_identity_principal_id

    monkeypatch.setenv("SHARED_IDENTITY_PRINCIPAL_ID", "  mi-oid-123  ")
    assert dashboard_identity_principal_id() == "mi-oid-123"
    monkeypatch.delenv("SHARED_IDENTITY_PRINCIPAL_ID", raising=False)
    assert dashboard_identity_principal_id() == ""

