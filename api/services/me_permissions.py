"""Effective RBAC permissions for the calling user at a given scope.

Responsibility: Compute a structured `CallerPermissions` payload the SPA
    can use to disable buttons (Start/Stop/Delete/Submit/Build) when the
    signed-in user lacks the underlying Azure RBAC role at the requested
    scope. Read-only ARM enumeration; never grants, never modifies.
Edit boundaries: ARM client wiring lives here; HTTP routing and response
    shaping live in `api.routes.me`. Role GUIDs / capability mapping is
    intentionally local so a future custom role can be added in one place.
Key entry points: `compute_caller_permissions`.
Risky contracts: Returns ``degraded=True`` with all capabilities ``True``
    when role enumeration fails. The SPA must treat this as "do not
    disable" so a transient ARM hiccup does not lock the operator out
    of legitimate actions. Real authorization is enforced server-side
    at submit time by the underlying ARM call; this surface is a UX
    affordance, not a security boundary.
Validation: `uv run pytest -q api/tests/test_me_permissions.py`.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any

from azure.core.credentials import TokenCredential

LOGGER = logging.getLogger(__name__)

# Entra/Azure AD object id format is a strict UUID. We pin this here so
# the OData ``filter`` interpolation below cannot smuggle additional
# clauses (critique-round-1 C5). Real callers come from a validated JWT
# whose ``oid`` is already a UUID, so this guard only kicks in for
# malformed test fixtures or a future caller that bypasses the JWT
# pipeline.
_OID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

# Built-in role definition GUIDs (stable across tenants).
# https://learn.microsoft.com/azure/role-based-access-control/built-in-roles
_ROLE_OWNER = "8e3af657-a8ff-443c-a75c-2fe8c4bcb635"
_ROLE_CONTRIBUTOR = "b24988ac-6180-42a0-ab88-20f7382dd24c"
_ROLE_READER = "acdd72a7-3385-48ef-bd42-f606fba81ae7"
_ROLE_USER_ACCESS_ADMINISTRATOR = "18d7d88d-d35e-4fb5-a5c3-7773c20a72d9"
# AKS-specific built-ins.
_ROLE_AKS_RBAC_ADMIN = "3498e952-d568-435e-9b2c-8d77e338d7f7"
_ROLE_AKS_RBAC_CLUSTER_ADMIN = "b1ff04bb-8a4e-4dc4-8eb5-8693973ce19b"
_ROLE_AKS_RBAC_READER = "7f6c6a51-bcf8-42ba-9220-52d62157d7db"
_ROLE_AKS_RBAC_WRITER = "a7ffa36f-339b-4b5c-8bdf-e2c188b2c0eb"
_ROLE_AKS_CONTRIBUTOR = "ed7f3fbd-7b88-4dd4-9017-9adb7ce333f8"
_ROLE_AKS_CLUSTER_USER = "4abbcc35-e782-43d8-92c5-2d3f1bd2253f"
# Storage built-ins (BLAST DB upload, query/result reads).
_ROLE_STORAGE_BLOB_DATA_CONTRIBUTOR = "ba92f5b4-2d11-453d-a403-e96b0029c9fe"
_ROLE_STORAGE_BLOB_DATA_READER = "2a2b9908-6ea1-4ae2-8e65-a410df84e7d1"

_WRITE_ROLES = frozenset(
    {
        _ROLE_OWNER,
        _ROLE_CONTRIBUTOR,
        _ROLE_AKS_CONTRIBUTOR,
        _ROLE_AKS_RBAC_ADMIN,
        _ROLE_AKS_RBAC_CLUSTER_ADMIN,
        _ROLE_AKS_RBAC_WRITER,
    }
)
_READ_ROLES = frozenset(
    {
        _ROLE_OWNER,
        _ROLE_CONTRIBUTOR,
        _ROLE_READER,
        _ROLE_AKS_CONTRIBUTOR,
        _ROLE_AKS_RBAC_ADMIN,
        _ROLE_AKS_RBAC_CLUSTER_ADMIN,
        _ROLE_AKS_RBAC_READER,
        _ROLE_AKS_RBAC_WRITER,
        _ROLE_AKS_CLUSTER_USER,
        # User Access Administrator has * on roleAssignments which
        # implies subscription read.
        _ROLE_USER_ACCESS_ADMINISTRATOR,
        _ROLE_STORAGE_BLOB_DATA_CONTRIBUTOR,
        _ROLE_STORAGE_BLOB_DATA_READER,
    }
)
# Only Owner / AKS RBAC Cluster Admin can delete a cluster.
_DELETE_ROLES = frozenset({_ROLE_OWNER, _ROLE_AKS_RBAC_CLUSTER_ADMIN})
# AKS Start/Stop is a ``Microsoft.ContainerService/managedClusters/start/action`` /
# ``.../stop/action``. Contributor on the cluster RG, AKS Contributor on the
# cluster, or Owner all satisfy this.
_START_STOP_ROLES = _WRITE_ROLES
# Submitting a BLAST job (= writing to the cluster's workload pool +
# Storage queries container) needs cluster write + storage write.
_SUBMIT_ROLES = _WRITE_ROLES | frozenset({_ROLE_STORAGE_BLOB_DATA_CONTRIBUTOR})
# Building an ACR image / triggering a deploy needs cluster write.
_BUILD_ROLES = _WRITE_ROLES
# Granting RBAC needs Owner or User Access Administrator.
_GRANT_RBAC_ROLES = frozenset({_ROLE_OWNER, _ROLE_USER_ACCESS_ADMINISTRATOR})


@dataclass(frozen=True)
class CallerPermissions:
    """Effective capabilities for the calling user at a scope.

    ``degraded=True`` means the enumeration call failed (caller lacks
    ``roleAssignments/read``, ARM hiccup, etc.) and every capability
    has been opened (``True``) so the SPA does not lock the operator
    out on a false negative. Real authorization is still enforced at
    submit time by ARM / Storage.
    """

    can_read: bool
    can_write: bool
    can_start_stop: bool
    can_delete: bool
    can_submit_blast: bool
    can_build_acr: bool
    can_grant_rbac: bool
    degraded: bool
    # Diagnostic: short list of role GUIDs that matched the caller at
    # the requested scope (or any ancestor scope). Empty when the
    # caller has none. SPA uses this for the tooltip ("you have:
    # Reader; need: Contributor").
    matched_roles: tuple[str, ...]
    # Human-friendly version of ``matched_roles`` for the SPA tooltip.
    # Best-effort: GUIDs we recognise are mapped to display names;
    # unknown GUIDs stay as GUIDs.
    matched_role_names: tuple[str, ...]
    # Short hint string ("no rbac assignments at this scope", ARM
    # error class, etc.) when degraded=True or when no roles matched.
    reason: str

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["matched_roles"] = list(self.matched_roles)
        out["matched_role_names"] = list(self.matched_role_names)
        return out


_ROLE_DISPLAY_NAMES: dict[str, str] = {
    _ROLE_OWNER: "Owner",
    _ROLE_CONTRIBUTOR: "Contributor",
    _ROLE_READER: "Reader",
    _ROLE_USER_ACCESS_ADMINISTRATOR: "User Access Administrator",
    _ROLE_AKS_RBAC_ADMIN: "Azure Kubernetes Service RBAC Admin",
    _ROLE_AKS_RBAC_CLUSTER_ADMIN: "Azure Kubernetes Service RBAC Cluster Admin",
    _ROLE_AKS_RBAC_READER: "Azure Kubernetes Service RBAC Reader",
    _ROLE_AKS_RBAC_WRITER: "Azure Kubernetes Service RBAC Writer",
    _ROLE_AKS_CONTRIBUTOR: "Azure Kubernetes Service Contributor Role",
    _ROLE_AKS_CLUSTER_USER: "Azure Kubernetes Service Cluster User Role",
    _ROLE_STORAGE_BLOB_DATA_CONTRIBUTOR: "Storage Blob Data Contributor",
    _ROLE_STORAGE_BLOB_DATA_READER: "Storage Blob Data Reader",
}


# Cache: keyed by ``(caller_oid, scope)``, TTL 60 s. The same cache is
# shared across uvicorn workers via process-local memory only; a future
# enhancement could promote this to Redis like the autostop status
# cache, but RBAC enumeration is rare enough (once per page load per
# scope per user) that local cache is sufficient.
_PERMS_CACHE_TTL_SECONDS = 60.0
_PERMS_CACHE: dict[tuple[str, str], tuple[float, CallerPermissions]] = {}
_PERMS_CACHE_LOCK = threading.Lock()
_PERMS_CACHE_MAX = 1024


def reset_permissions_cache_for_tests() -> None:
    """Test hook \u2014 drop every cached permission row."""
    with _PERMS_CACHE_LOCK:
        _PERMS_CACHE.clear()


def _build_scope(
    subscription_id: str,
    resource_group: str | None,
    cluster_name: str | None,
) -> str:
    sub = (subscription_id or "").strip()
    if not sub:
        raise ValueError("subscription_id required")
    scope = f"/subscriptions/{sub}"
    if resource_group:
        scope += f"/resourceGroups/{resource_group}"
        if cluster_name:
            scope += (
                "/providers/Microsoft.ContainerService/managedClusters/"
                f"{cluster_name}"
            )
    return scope


def _is_ancestor_or_equal(assignment_scope: str, target_scope: str) -> bool:
    """Return True when ``assignment_scope`` is the same as ``target_scope``
    or one of its parent paths. Lowercase comparison so Azure's mixed
    casing does not cause false negatives.

    The scope hierarchy Azure inherits down is::

        tenant root "/"  >  management group  >  subscription  >  rg  >  resource

    The ``/`` and management-group tiers are NOT string prefixes of a
    ``/subscriptions/...`` target, so a naive ``startswith`` check drops
    them and a subscription Owner who holds the role through a management
    group (or tenant-root) assignment is wrongly seen as having no role.
    Because the enumeration that produced ``assignment_scope`` is already
    scoped to this subscription (``list_for_subscription`` + ``assignedTo``),
    any root/management-group assignment returned here is, by construction,
    inherited by the target subscription and everything below it — so we
    accept it for any subscription-or-below target without re-walking the
    management-group hierarchy."""
    raw_a = (assignment_scope or "").strip()
    a = assignment_scope.lower().rstrip("/")
    t = target_scope.lower().rstrip("/")
    if not t:
        return False
    # Tenant root scope ("/") is an ancestor of every resource. ``rstrip``
    # collapses it to an empty string, so detect it on the raw value before
    # the generic empty-guard below rejects it.
    if raw_a == "/":
        return True
    if not a:
        return False
    if a == t:
        return True
    # Management-group scope is an ancestor of any subscription-or-below
    # target (see the docstring: the enumeration is already sub-scoped, so
    # only MGs this subscription inherits are returned).
    if a.startswith("/providers/microsoft.management/managementgroups/"):
        return True
    return t.startswith(a + "/")


def _enumerate_role_assignments(
    credential: TokenCredential,
    subscription_id: str,
    caller_oid: str,
) -> tuple[list[tuple[str, str]], str | None]:
    """List the caller's role assignments scoped to the subscription.

    Returns ``(rows, error)`` where each row is ``(role_guid_lower,
    scope_lower)``. On enumeration failure returns ``([], reason)`` so
    the caller can produce a ``degraded=True`` response.

    Critique-round-1 C5: ``caller_oid`` is interpolated into an OData
    ``filter`` string and ARM is not the strongest defence against an
    injection here (a malformed oid could escape the quotes and append
    additional clauses). Real callers come from a validated JWT whose
    ``oid`` is already a UUID, but we reject anything non-UUID
    defensively so a future caller that bypasses the JWT layer cannot
    smuggle OData.

    Filter choice: ``assignedTo('{oid}')`` (NOT ``principalId eq
    '{oid}'``). ``principalId eq`` only matches assignments whose
    principal IS the caller's own object id, so a user who holds Reader
    /Contributor purely through Entra **group** membership (the
    assignment's principal is the group's object id) gets ZERO rows and
    is wrongly treated as having no role. ``assignedTo()`` is the filter
    the Azure CLI uses for ``--include-groups``: it returns the user's
    direct assignments AND every assignment inherited transitively from
    the groups the user belongs to, which is what "effective access"
    means here.
    """
    if not _OID_RE.match(caller_oid):
        return [], "invalid_oid_format"

    try:
        from azure.mgmt.authorization import AuthorizationManagementClient
    except Exception as exc:
        return [], f"sdk_import_failed:{type(exc).__name__}"

    try:
        client = AuthorizationManagementClient(credential, subscription_id)
        rows: list[tuple[str, str]] = []
        # ``list_for_subscription`` + ``assignedTo()`` includes inherited
        # assignments from tenant root / management groups AND those the
        # caller holds transitively through Entra group membership, which
        # is exactly what we want to evaluate effective access at
        # sub-or-below scopes.
        for r in client.role_assignments.list_for_subscription(
            filter=f"assignedTo('{caller_oid}')"
        ):
            role_def_id = (getattr(r, "role_definition_id", None) or "").lower()
            scope = (getattr(r, "scope", None) or "").lower()
            guid = role_def_id.rsplit("/", 1)[-1]
            if guid and scope:
                rows.append((guid, scope))
        return rows, None
    except Exception as exc:
        return [], f"{type(exc).__name__}: {str(exc)[:160]}"


def compute_caller_permissions(
    credential: TokenCredential,
    *,
    caller_oid: str,
    subscription_id: str,
    resource_group: str | None = None,
    cluster_name: str | None = None,
) -> CallerPermissions:
    """Resolve the calling user's effective capabilities at a scope.

    Used by `/api/me/permissions` so the SPA can grey out actions the
    user cannot perform AND show a tooltip explaining what role they
    would need. NEVER a security boundary \u2014 ARM still enforces at
    submit time; this is a UX affordance.
    """
    if not caller_oid:
        return CallerPermissions(
            can_read=False,
            can_write=False,
            can_start_stop=False,
            can_delete=False,
            can_submit_blast=False,
            can_build_acr=False,
            can_grant_rbac=False,
            degraded=False,
            matched_roles=(),
            matched_role_names=(),
            reason="no_caller_oid",
        )
    try:
        scope = _build_scope(subscription_id, resource_group, cluster_name)
    except ValueError as exc:
        return CallerPermissions(
            can_read=False,
            can_write=False,
            can_start_stop=False,
            can_delete=False,
            can_submit_blast=False,
            can_build_acr=False,
            can_grant_rbac=False,
            degraded=False,
            matched_roles=(),
            matched_role_names=(),
            reason=f"invalid_scope:{exc}",
        )

    cache_key = (caller_oid, scope.lower())
    now = time.monotonic()
    with _PERMS_CACHE_LOCK:
        cached = _PERMS_CACHE.get(cache_key)
        if cached is not None and cached[0] > now:
            return cached[1]

    rows, err = _enumerate_role_assignments(credential, subscription_id, caller_oid)
    if err:
        # Degrade open: SPA must not lock the operator out on a
        # transient enumeration failure. ARM enforces real authorization.
        LOGGER.warning(
            "me.compute_caller_permissions enumerate failed scope=%s err=%s",
            scope,
            err,
        )
        result = CallerPermissions(
            can_read=True,
            can_write=True,
            can_start_stop=True,
            can_delete=True,
            can_submit_blast=True,
            can_build_acr=True,
            can_grant_rbac=True,
            degraded=True,
            matched_roles=(),
            matched_role_names=(),
            reason=err,
        )
        _store_in_cache(cache_key, result)
        return result

    matched: list[str] = []
    for guid, assignment_scope in rows:
        if _is_ancestor_or_equal(assignment_scope, scope):
            matched.append(guid)

    matched_set = frozenset(matched)
    result = CallerPermissions(
        can_read=bool(matched_set & _READ_ROLES),
        can_write=bool(matched_set & _WRITE_ROLES),
        can_start_stop=bool(matched_set & _START_STOP_ROLES),
        can_delete=bool(matched_set & _DELETE_ROLES),
        can_submit_blast=bool(matched_set & _SUBMIT_ROLES),
        can_build_acr=bool(matched_set & _BUILD_ROLES),
        can_grant_rbac=bool(matched_set & _GRANT_RBAC_ROLES),
        degraded=False,
        matched_roles=tuple(sorted(matched_set)),
        matched_role_names=tuple(
            sorted(
                _ROLE_DISPLAY_NAMES.get(guid, guid) for guid in matched_set
            )
        ),
        reason="" if matched_set else "no_role_at_scope",
    )
    _store_in_cache(cache_key, result)
    return result


def _store_in_cache(
    cache_key: tuple[str, str], result: CallerPermissions
) -> None:
    expires_at = time.monotonic() + _PERMS_CACHE_TTL_SECONDS
    with _PERMS_CACHE_LOCK:
        _PERMS_CACHE[cache_key] = (expires_at, result)
        # Trim the oldest entry when we exceed the soft cap. Sufficient for
        # bounded growth; per-user-per-scope churn is low so an O(n) sweep
        # on overflow is fine.
        if len(_PERMS_CACHE) > _PERMS_CACHE_MAX:
            oldest_key = min(_PERMS_CACHE, key=lambda k: _PERMS_CACHE[k][0])
            _PERMS_CACHE.pop(oldest_key, None)
