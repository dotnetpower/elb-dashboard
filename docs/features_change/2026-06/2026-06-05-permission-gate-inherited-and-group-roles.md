---
title: Permission gate recognises management-group / tenant-root and group-inherited roles
description: A subscription Owner who holds the role through a management group, the tenant root, or an Entra group is no longer wrongly told they have no Azure RBAC role at the scope.
tags:
  - auth
  - ui
---

# Permission gate recognises inherited and group-granted roles

## Motivation

The SPA disables Start/Stop/Delete/Submit/Build buttons and shows a tooltip
such as:

> You do not have permission to delete this cluster. You hold: no Azure RBAC
> role at this scope. You need: Owner or Azure Kubernetes Service RBAC Cluster
> Admin.

This is driven by `GET /api/me/permissions`
([api/services/me_permissions.py](../../../api/services/me_permissions.py)),
which enumerates the caller's role assignments and matches them against the
target scope. It is a **UX affordance, not a security boundary** — ARM still
enforces real authorization at submit time.

A subscription **Owner** in another tenant was shown the "no role" tooltip and
had Delete disabled even though they could delete the cluster. Reasoning from
the code (the affected tenant could not be inspected directly) surfaced two
independent defects, either of which reproduces the exact symptom
(`degraded=false`, zero matched roles):

1. **Group-granted roles were invisible.** The enumeration filtered with
   `principalId eq '<oid>'`, which only matches assignments whose principal is
   the caller's own object id. An Owner granted through an Entra **security
   group** (the assignment's principal is the group's object id) produced zero
   rows.
2. **Management-group / tenant-root inheritance was dropped.** Azure inherits
   roles down `tenant root "/" > management group > subscription > rg >
   resource`. The ancestor check `_is_ancestor_or_equal` used a plain
   `startswith` prefix test, so an assignment at
   `/providers/Microsoft.Management/managementGroups/<id>` (not a string prefix
   of `/subscriptions/<sub>/...`) and the tenant root `/` (collapsed to an
   empty string by `rstrip('/')` and then rejected by the empty-guard) were
   both discarded. The tenant's global admins — typically Owners at the
   management-group or tenant-root tier — fell straight into the "no role"
   branch.

## User-facing change

* A subscription Owner (or any RBAC role) granted via an **Entra group**, a
  **management group**, or the **tenant root** now sees the correct enabled
  buttons and a tooltip listing the role they actually hold.
* No change for users who already held the role directly at the subscription /
  resource-group / resource scope — they were unaffected and stay unaffected.
* Still a UX affordance only: enumeration failures continue to **degrade open**
  (`degraded=true`, all capabilities `true`) and ARM remains the real
  enforcement point at submit time.

## API / IaC diff summary

* [api/services/me_permissions.py](../../../api/services/me_permissions.py)
  * `_enumerate_role_assignments` now filters with `assignedTo('<oid>')`
    instead of `principalId eq '<oid>'`. `assignedTo()` is the filter the
    Azure CLI `--include-groups` uses; it expands transitive group membership
    and returns direct + inherited assignments. The UUID guard on `caller_oid`
    is unchanged, so the OData interpolation stays injection-safe.
  * `_is_ancestor_or_equal` now accepts a tenant-root (`/`) assignment as an
    ancestor of any target, and a management-group-scope assignment as an
    ancestor of any subscription-or-below target. The enumeration is already
    subscription-scoped (`list_for_subscription`), so only management groups
    the subscription actually inherits are returned — no management-group
    hierarchy walk is needed.
* [api/services/access_review.py](../../../api/services/access_review.py) — the
  "View my access" per-resource-group panel shares the same enumeration. It
  already filtered with `assignedTo()` and already accepted management-group
  scopes, but its `_is_ancestor_or_equal` dropped the **tenant-root `/`** scope
  (the `rstrip('/')` empty-string gap). Fixed for parity so a tenant-root Owner
  is shown in the panel too.
* Response schema is unchanged (same `CallerPermissions` fields), so the SPA
  `usePermissions` hook and `PermissionGate` component need no changes.
* No IaC change.

## Validation evidence

* `uv run pytest -q api/tests/test_me_permissions.py` — 15 passed, including
  the new cases:
  * `test_enumeration_uses_assigned_to_filter_for_group_transitivity`
  * `test_group_inherited_reader_is_recognized`
  * `test_owner_inherited_from_management_group_grants_delete`
  * `test_owner_inherited_from_tenant_root_grants_delete`
* `uv run pytest -q api/tests/test_access_review.py` — includes the new
  `test_tenant_root_assignment_inherited`.
* `uv run pytest -q api/tests/test_me_permissions.py api/tests/test_access_review.py api/tests/test_me_route.py api/tests/test_persona_matrix.py`
  — 76 passed (no persona-matrix regression).
* `uv run ruff check api/services/me_permissions.py api/services/access_review.py api/tests/test_me_permissions.py api/tests/test_access_review.py`
  — clean.
* Real-tenant cross-check (`az rest` against the maintainer's own
  subscription): the `assignedTo('<oid>')` filter returns the tenant-root
  (`/`) and management-group Owner assignments that `principalId eq` also
  returned, confirming the filter is a superset and the ancestor logic now
  matches them.
