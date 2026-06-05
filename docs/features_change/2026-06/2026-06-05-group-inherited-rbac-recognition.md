---
title: Recognize group-inherited RBAC in caller permissions
description: The caller permission resolver now expands transitive Entra group membership so users granted Reader/Contributor via a group are no longer treated as having no role.
tags:
  - auth
  - security
---

# Recognize group-inherited RBAC in caller permissions

## Motivation

A user whose `Reader` (or `Contributor`) on the workload resource group was
granted through an Entra **group** — not a direct assignment to their own
object id — was treated by the dashboard as having no Azure role at the
scope. Every gated action button was disabled and the permission tooltip read
"no Azure RBAC role at this scope", even though the user could read the
resources via the group.

Root cause: `api/services/me_permissions.py` enumerated the caller's role
assignments with the OData filter `principalId eq '{oid}'`. That filter only
matches assignments whose principal IS the caller's own object id. A
group-inherited assignment carries the **group's** object id as its principal,
so the query returned zero rows and the resolver concluded
`no_role_at_scope`.

This mirrors the difference between `az role assignment list --assignee <oid>`
(default, direct-only) and `az role assignment list --assignee <oid>
--include-groups` (expands transitive group membership).

## User-Facing Change

- Users who hold Reader / Contributor / Owner / Storage / AKS roles purely
  through Entra group membership now see the correct effective permissions:
  read-only surfaces load and write actions are enabled according to the
  group's role, instead of every gated button being disabled.
- No change for users with direct role assignments (the new filter is a strict
  superset of the old behaviour).

## API/IaC Diff Summary

- `api/services/me_permissions.py`: `_enumerate_role_assignments` now calls
  `role_assignments.list_for_subscription(filter="assignedTo('{oid}')")`
  instead of `principalId eq '{oid}'`. The `_OID_RE` UUID guard is unchanged,
  so the OData-injection defence (critique-round-1 C5) is preserved — the oid
  is still validated before interpolation.
- No response-shape change: `/api/me/permissions` returns the same
  `CallerPermissions` fields; only the underlying enumeration is broader.
- The managed-identity preflights (`api/services/rbac_preflight.py`,
  `api/services/k8s/prepare_db_preflight.py`) intentionally keep
  `principalId eq` — they resolve the shared MI / AKS kubelet identity whose
  roles Bicep assigns directly, so direct-only enumeration is correct there.
- No infrastructure changes.

## Validation Evidence

- `uv run pytest -q api/tests/test_me_permissions.py api/tests/test_me_route.py`
  — 19 passed, including two new tests:
  - `test_enumeration_uses_assigned_to_filter_for_group_transitivity` pins the
    filter string to `assignedTo('<oid>')`.
  - `test_group_inherited_reader_is_recognized` asserts a group-surfaced Reader
    assignment yields `can_read=True` instead of `no_role_at_scope`.
- `uv run pytest -q api/tests/test_rbac_preflight.py api/tests/test_me_permissions.py api/tests/test_me_route.py api/tests/test_persona_matrix.py`
  — 74 passed (persona-matrix security regression gate green).
- `uv run ruff check api/services/me_permissions.py api/tests/test_me_permissions.py`
  — all checks passed.
