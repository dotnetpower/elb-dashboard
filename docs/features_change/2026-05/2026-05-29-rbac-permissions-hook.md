# 2026-05-29 — RBAC-aware UI: `/api/me/permissions` + `<PermissionGate>` (#6)

## Motivation

User requirement #6 from the project review:

> RBAC-driven disabled UI with tooltip — a user who lacks Contributor
> on a cluster RG should see Start/Stop/Delete/Submit/Build buttons
> already disabled with a tooltip explaining which role they need,
> not click through to a silent 403.

Previously the only feedback was a toast after the failed PUT, by
which time the user had already wondered whether the click registered.
This change adds a backend endpoint that resolves the calling user's
effective Azure RBAC capabilities at a scope, a React Query hook that
caches the answer for 60 s, and a `<PermissionGate>` component that
disables (or hides) a clickable element with a "you have X, you need
Y" tooltip.

## User-facing change

- **New endpoint** `GET /api/me/permissions?subscription_id=…[&resource_group=…&cluster_name=…]`
  returns a structured capability shape (`can_read`, `can_write`,
  `can_start_stop`, `can_delete`, `can_submit_blast`, `can_build_acr`,
  `can_grant_rbac`, `degraded`, `matched_roles`,
  `matched_role_names`, `reason`). Cached server-side for 60 s per
  `(caller_oid, scope)` pair.
- **Auto-stop panel** now disables the Enable / Idle-minutes /
  Extend controls for users without `Contributor` (or equivalent)
  on the cluster RG, with a tooltip explaining the missing role.
  Read-only users (Reader at sub) see the panel but cannot mutate
  it; they still see the live verdict / countdown.
- **Degrade-open behaviour**: if the role enumeration call itself
  fails (ARM hiccup, caller lacks `roleAssignments/read`, …), every
  capability is set to `true` and `degraded=true` is returned. The
  SPA must not lock the operator out on a transient backend error —
  ARM still enforces real authorization at submit time. This is a
  UX affordance, not a security boundary.

## API / IaC diff summary

### Backend (`api/`)

| File | Change |
|---|---|
| [api/services/me_permissions.py](../../../api/services/me_permissions.py) | New — role GUID → capability mapping; `compute_caller_permissions(credential, caller_oid, sub, rg=None, cluster=None)`; 60 s LRU-bounded cache (1024 entries); degrade-open on enumeration failure |
| [api/routes/me.py](../../../api/routes/me.py) | New `GET /me/permissions` route |
| [api/tests/test_me_permissions.py](../../../api/tests/test_me_permissions.py) | New — 10 tests covering Owner/Reader/Contributor/UAA mapping, scope-inheritance direction (descendant scope must NOT bubble up), cache behaviour, empty-oid path, degrade-open path |
| [api/tests/test_me_route.py](../../../api/tests/test_me_route.py) | +2 tests for the new route (shape contract + 422 missing-query validation) |

### Frontend (`web/`)

| File | Change |
|---|---|
| [web/src/api/me.ts](../../../web/src/api/me.ts) | `CallerPermissionsResponse` interface + `meApi.permissions(subId, rg?, cluster?)` typed client |
| [web/src/hooks/usePermissions.ts](../../../web/src/hooks/usePermissions.ts) | New — `usePermissions(subId, rg?, cluster?)` returns `{ permissions, isLoading, isError, error }`; fallback `OPEN_PERMISSIONS` keeps every flag true while loading / errored (matches backend degrade-open contract) |
| [web/src/components/PermissionGate.tsx](../../../web/src/components/PermissionGate.tsx) | New — `<PermissionGate need="can_*" permissions={…}>{children}</PermissionGate>` disables the wrapped element (or hides it via `hideInsteadOfDisable`) with a `"You do not have permission to X. You hold: Y. You need: Z."` tooltip when the capability is false. Stays open when `degraded=true` |
| [web/src/components/PermissionGate.test.ts](../../../web/src/components/PermissionGate.test.ts) | New — 5 tests pinning `permissionDeniedTooltip` for every capability |
| [web/src/components/ClusterItem/AutoStopPanel.tsx](../../../web/src/components/ClusterItem/AutoStopPanel.tsx) | Wraps the Enable checkbox, the Idle-minutes select, and the Extend button in `<PermissionGate need="can_write" permissions={…}>` (first consumer; serves as the wiring template for future RBAC-gated controls) |

### IaC

No infra changes in this wave.

## Validation evidence

```text
$ uv run pytest -q api/tests
............................................................... [100%]
1898 passed, 3 skipped in 33.70s

$ cd web && npm test -- --run
 Test Files  56 passed (56)
      Tests  433 passed (433)

$ uv run ruff check api
All checks passed!

$ cd web && npm run build
✓ built in 7.39s
```

## Self-review

- Consumer search for `meApi.get` confirmed adding `meApi.permissions`
  is a pure addition; no existing call site touched.
- Consumer search for `<PermissionGate>` confirmed AutoStopPanel is the
  only current wiring; charts a clear template for the next RBAC-gated
  surface (Submit, Start/Stop, Delete buttons in subsequent waves).
- The fallback `OPEN_PERMISSIONS` in `usePermissions.ts` carries
  `degraded: true` so the `<PermissionGate>` early-return on degraded
  stays open during the initial network round-trip; no flash-of-disabled
  state.
- Cache TTL (60 s server, 60 s client) keeps role-list ARM calls below
  one per page per scope per minute. The cache key shape mirrors the
  ancestor-inclusion test so cross-scope mistakes are caught.
- Type contract: `CallerPermissionsResponse` matches the backend
  `CallerPermissions.to_dict()` exactly; the new route test pins every
  key so a renamed field on either side fails CI.

## Not covered in this wave (followups)

- Wiring `<PermissionGate>` to the cluster Start/Stop/Delete buttons,
  the BLAST Submit button, and the ACR build trigger.
- Promoting the in-process permissions cache to Redis (matches
  autostop status #18; deferred until polling load actually warrants
  it — current usage is once per page).
- A dedicated `<PermissionTooltip>` variant for elements that cannot
  accept `disabled` (e.g. anchor links).
