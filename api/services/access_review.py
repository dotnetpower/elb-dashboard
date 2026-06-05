"""Per-resource-group RBAC access review for the calling user.

Responsibility: Reproduce the Azure portal "View my access" experience for a
    curated list of resource groups. Enumerate the signed-in caller's effective
    role assignments once at subscription scope (direct + Entra-group-inherited),
    then group them per target resource group with inheritance metadata so the
    SPA can render an IAM-style table when diagnosing tenant permission gaps.
    Read-only ARM enumeration; never grants, never modifies.
Edit boundaries: ARM client wiring + role-name resolution live here; HTTP
    routing and response shaping live in `api.routes.me`. Capability mapping
    (can_write/etc.) intentionally stays in `me_permissions` — this module is a
    diagnostic listing, not a permission gate.
Key entry points: `review_resource_group_access`, `dashboard_identity_principal_id`.
Risky contracts: Unlike `compute_caller_permissions`, this surface does NOT
    degrade open. When enumeration fails (caller lacks
    `Microsoft.Authorization/roleAssignments/read`) every group is marked
    ``degraded=True`` with an explicit reason so the operator sees the real
    permission gap instead of a fabricated "you have access". The OData filter
    interpolates ``caller_oid`` which is UUID-validated up front to block
    injection. Resource-group names are validated against `_RG_NAME_RE` before
    use.
Validation: `uv run pytest -q api/tests/test_access_review.py`.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any

from azure.core.credentials import TokenCredential

from api.services.me_permissions import _OID_RE, _ROLE_DISPLAY_NAMES

LOGGER = logging.getLogger(__name__)

# Azure resource-group names: 1-90 chars, letters/digits/underscore/period/
# hyphen/parens. Validated before interpolating into the ARM resource id so a
# crafted name cannot smuggle path segments.
_RG_NAME_RE = re.compile(r"^[-\w._()]{1,90}$")

# Cache the whole review keyed by (oid, sub, sorted-rgs). RBAC enumeration is
# rare (once per Settings open) so a short process-local TTL is sufficient.
_REVIEW_CACHE_TTL_SECONDS = 60.0
_REVIEW_CACHE: dict[tuple[str, str, tuple[str, ...]], tuple[float, AccessReview]] = {}
_REVIEW_CACHE_LOCK = threading.Lock()
_REVIEW_CACHE_MAX = 256

# Resolved role-definition-id -> display name, shared across calls within a
# process. Seeded with the well-known built-ins so the common case needs zero
# extra ARM round-trips; unknown / custom roles are resolved lazily and cached.
_ROLE_NAME_CACHE: dict[str, str] = {}
_ROLE_NAME_CACHE_LOCK = threading.Lock()


@dataclass(frozen=True)
class RoleAssignmentRow:
    """One effective role assignment as seen from a target resource group."""

    role_name: str
    role_guid: str
    # Where the assignment actually lives: "subscription",
    # "management_group", "resource_group", or "resource".
    scope_level: str
    # True when the assignment lives at a broader scope than the target RG
    # (i.e. the RG inherits it) — mirrors the portal's "(Inherited)" label.
    inherited: bool
    # Full ARM scope of the assignment, lowercased.
    assignment_scope: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ResourceGroupAccess:
    """Effective access for the caller at one resource group."""

    resource_group: str
    scope: str
    assignments: tuple[RoleAssignmentRow, ...]
    # True when enumeration failed for this review (no usable data). The SPA
    # must NOT treat this as "has access" — it means the caller likely lacks
    # roleAssignments/read, which is itself a finding.
    degraded: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "resource_group": self.resource_group,
            "scope": self.scope,
            "assignments": [a.to_dict() for a in self.assignments],
            "degraded": self.degraded,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ReviewPrincipal:
    """Whose access a review describes."""

    # "user" (the signed-in caller) or "dashboard_identity" (the shared
    # user-assigned managed identity the Container App runs as).
    kind: str
    object_id: str
    # False when the principal could not be resolved (e.g. the dashboard
    # managed identity's principal id is not exported in a local-dev shell).
    available: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AccessReview:
    """Full per-RG access review for one subscription."""

    subscription_id: str
    principal: ReviewPrincipal
    groups: tuple[ResourceGroupAccess, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "subscription_id": self.subscription_id,
            "principal": self.principal.to_dict(),
            "groups": [g.to_dict() for g in self.groups],
        }


def reset_access_review_cache_for_tests() -> None:
    """Test hook — drop every cached review and resolved role name."""
    with _REVIEW_CACHE_LOCK:
        _REVIEW_CACHE.clear()
    with _ROLE_NAME_CACHE_LOCK:
        _ROLE_NAME_CACHE.clear()


def dashboard_identity_principal_id() -> str:
    """Return the dashboard managed identity's principal (object) id.

    The Container App template exports ``SHARED_IDENTITY_PRINCIPAL_ID`` for
    the shared user-assigned managed identity every sidecar runs as. Returns
    an empty string when it is not set (a local-dev shell with no MI), in
    which case the access review reports ``principal.available=False``.
    """
    return (os.environ.get("SHARED_IDENTITY_PRINCIPAL_ID") or "").strip()


def _scope_level(assignment_scope: str) -> str:
    """Classify an ARM scope path into a coarse level for the UI."""
    s = assignment_scope.lower().rstrip("/")
    if "/providers/microsoft.management/managementgroups/" in s:
        return "management_group"
    if "/resourcegroups/" in s:
        # A resource lives under .../resourceGroups/<rg>/providers/...
        if "/providers/" in s.split("/resourcegroups/", 1)[1]:
            return "resource"
        return "resource_group"
    if s.startswith("/subscriptions/"):
        return "subscription"
    return "other"


def _is_ancestor_or_equal(assignment_scope: str, target_scope: str) -> bool:
    """True when ``assignment_scope`` equals ``target_scope`` or is a parent.

    Lowercase comparison so Azure's mixed casing never causes a false
    negative. A management-group assignment that the subscription descends
    from is also treated as an ancestor (handled by the caller, which passes
    such rows through because list_for_subscription already filters to scopes
    relevant to this subscription). The tenant root scope ``/`` is likewise an
    ancestor of every target; ``rstrip('/')`` collapses it to an empty string,
    so it is detected on the raw value before the generic empty-guard rejects
    it.
    """
    raw_a = (assignment_scope or "").strip()
    a = assignment_scope.lower().rstrip("/")
    t = target_scope.lower().rstrip("/")
    if not t:
        return False
    if raw_a == "/":
        return True
    if not a:
        return False
    if a == t:
        return True
    if t.startswith(a + "/"):
        return True
    # Management-group assignments surface with a scope that is NOT a string
    # prefix of the subscription path, but list_for_subscription only returns
    # assignments that apply to this subscription, so any management_group
    # row is by definition an ancestor of every RG in the subscription.
    if _scope_level(a) == "management_group":
        return True
    return False


def _enumerate(
    credential: TokenCredential,
    subscription_id: str,
    caller_oid: str,
) -> tuple[list[tuple[str, str, str]], str | None]:
    """Enumerate the caller's effective role assignments for the subscription.

    Returns ``(rows, error)`` where each row is
    ``(role_guid_lower, assignment_scope_lower, role_definition_id_lower)``.
    On failure returns ``([], reason)``.

    Uses ``assignedTo('{oid}')`` so Entra-group-inherited assignments are
    included (same semantics as the Azure CLI ``--include-groups``), matching
    what the portal "View my access" shows.
    """
    if not _OID_RE.match(caller_oid):
        return [], "invalid_oid_format"

    try:
        from azure.mgmt.authorization import AuthorizationManagementClient
    except Exception as exc:  # pragma: no cover - import guard
        return [], f"sdk_import_failed:{type(exc).__name__}"

    try:
        client = AuthorizationManagementClient(credential, subscription_id)
        rows: list[tuple[str, str, str]] = []
        for r in client.role_assignments.list_for_subscription(
            filter=f"assignedTo('{caller_oid}')"
        ):
            role_def_id = (getattr(r, "role_definition_id", None) or "").lower()
            scope = (getattr(r, "scope", None) or "").lower()
            guid = role_def_id.rsplit("/", 1)[-1]
            if guid and scope:
                rows.append((guid, scope, role_def_id))
        return rows, None
    except Exception as exc:
        return [], f"{type(exc).__name__}: {str(exc)[:160]}"


def _resolve_role_name(
    credential: TokenCredential,
    subscription_id: str,
    role_guid: str,
    role_definition_id: str,
) -> str:
    """Map a role definition to a display name, caching the result.

    Well-known built-ins resolve from the local table with zero ARM calls.
    Unknown / custom roles are looked up via ``role_definitions.get_by_id``
    once and cached for the process lifetime. Falls back to the GUID when the
    lookup fails so the UI always has something stable to show.
    """
    known = _ROLE_DISPLAY_NAMES.get(role_guid)
    if known:
        return known

    with _ROLE_NAME_CACHE_LOCK:
        cached = _ROLE_NAME_CACHE.get(role_definition_id)
    if cached is not None:
        return cached

    name = role_guid
    try:
        from azure.mgmt.authorization import AuthorizationManagementClient

        client = AuthorizationManagementClient(credential, subscription_id)
        definition = client.role_definitions.get_by_id(role_definition_id)
        name = getattr(definition, "role_name", None) or role_guid
    except Exception:
        name = role_guid

    with _ROLE_NAME_CACHE_LOCK:
        _ROLE_NAME_CACHE[role_definition_id] = name
    return name


def _build_rg_scope(subscription_id: str, resource_group: str) -> str:
    return (
        f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
    ).lower()


def _dedup_resource_groups(resource_groups: list[str]) -> list[str]:
    """Preserve order, drop blanks/dupes (case-insensitive)."""
    seen: set[str] = set()
    out: list[str] = []
    for rg in resource_groups:
        name = (rg or "").strip()
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(name)
    return out


def _store_in_cache(
    cache_key: tuple[str, str, tuple[str, ...]], review: AccessReview
) -> None:
    with _REVIEW_CACHE_LOCK:
        if len(_REVIEW_CACHE) >= _REVIEW_CACHE_MAX:
            _REVIEW_CACHE.clear()
        _REVIEW_CACHE[cache_key] = (
            time.monotonic() + _REVIEW_CACHE_TTL_SECONDS,
            review,
        )


def review_resource_group_access(
    credential: TokenCredential,
    *,
    principal_oid: str,
    subscription_id: str,
    resource_groups: list[str],
    principal_kind: str = "user",
) -> AccessReview:
    """Build a per-resource-group access review for one principal.

    ``principal_oid`` is the object id whose effective access is reviewed —
    the signed-in caller (``principal_kind="user"``) or the dashboard's
    shared managed identity (``principal_kind="dashboard_identity"``). One
    ARM enumeration call resolves that principal's effective assignments for
    the whole subscription; each requested RG is then matched against those
    assignments (direct or inherited). Invalid RG names are skipped with a
    ``degraded`` row rather than raising, so one bad entry never sinks the
    whole review.

    When ``principal_oid`` is empty (e.g. the dashboard managed identity's
    principal id is not available in a local-dev shell) the review returns
    ``principal.available=False`` and no groups, so the SPA can explain the
    gap instead of rendering a misleading "no access" table.
    """
    sub = (subscription_id or "").strip()
    oid = (principal_oid or "").strip()
    if not sub:
        return AccessReview(
            subscription_id="",
            principal=ReviewPrincipal(
                kind=principal_kind, object_id=oid, available=bool(oid)
            ),
            groups=(),
        )
    if not oid:
        return AccessReview(
            subscription_id=sub,
            principal=ReviewPrincipal(
                kind=principal_kind, object_id="", available=False
            ),
            groups=(),
        )

    principal = ReviewPrincipal(kind=principal_kind, object_id=oid, available=True)

    requested = _dedup_resource_groups(resource_groups)
    if not requested:
        return AccessReview(subscription_id=sub, principal=principal, groups=())

    cache_key = (oid, sub.lower(), tuple(r.lower() for r in requested))
    now = time.monotonic()
    with _REVIEW_CACHE_LOCK:
        cached = _REVIEW_CACHE.get(cache_key)
        if cached is not None and cached[0] > now:
            return cached[1]

    rows, err = _enumerate(credential, sub, oid)

    groups: list[ResourceGroupAccess] = []
    for rg in requested:
        rg_scope = _build_rg_scope(sub, rg)
        if not _RG_NAME_RE.match(rg):
            groups.append(
                ResourceGroupAccess(
                    resource_group=rg,
                    scope=rg_scope,
                    assignments=(),
                    degraded=True,
                    reason="invalid_resource_group_name",
                )
            )
            continue
        if err:
            LOGGER.warning(
                "access_review enumerate failed sub=%s rg=%s err=%s",
                sub,
                rg,
                err,
            )
            groups.append(
                ResourceGroupAccess(
                    resource_group=rg,
                    scope=rg_scope,
                    assignments=(),
                    degraded=True,
                    reason=err,
                )
            )
            continue

        matched: list[RoleAssignmentRow] = []
        seen_rows: set[tuple[str, str]] = set()
        for guid, assignment_scope, role_def_id in rows:
            if not _is_ancestor_or_equal(assignment_scope, rg_scope):
                continue
            dedup_key = (guid, assignment_scope)
            if dedup_key in seen_rows:
                continue
            seen_rows.add(dedup_key)
            matched.append(
                RoleAssignmentRow(
                    role_name=_resolve_role_name(
                        credential, sub, guid, role_def_id
                    ),
                    role_guid=guid,
                    scope_level=_scope_level(assignment_scope),
                    inherited=assignment_scope.rstrip("/") != rg_scope.rstrip("/"),
                    assignment_scope=assignment_scope,
                )
            )

        matched.sort(key=lambda a: (a.inherited, a.role_name.lower()))
        groups.append(
            ResourceGroupAccess(
                resource_group=rg,
                scope=rg_scope,
                assignments=tuple(matched),
                degraded=False,
                reason="" if matched else "no_role_at_scope",
            )
        )

    review = AccessReview(
        subscription_id=sub, principal=principal, groups=tuple(groups)
    )
    _store_in_cache(cache_key, review)
    return review
