---
title: Access review — per-resource-group "View my access" diagnostics
description: Settings → Diagnose & solve problems surfaces the signed-in user's effective Azure RBAC role assignments per resource group to debug tenant permission gaps.
tags:
  - auth
  - ui
---

# Access review — per-resource-group "View my access"

## Motivation

When the dashboard is stood up in a fresh tenant, the most common failure mode
is a missing Azure RBAC role on one of the resource groups the control plane
touches (workload/dashboard RG, ACR RG, terminal RG, AKS cluster RG). These
failures surface 30-60 s later as opaque `AuthorizationFailed` errors, and the
only way to diagnose them today is to open the Azure portal, navigate to each
resource group's **Access control (IAM) → View my access**, and eyeball the
inherited role assignments.

This change brings that portal experience into the dashboard's Settings panel
so an operator can spot the gap without leaving the app.

## User-facing change

New **Settings → Diagnose & solve problems** section, structured like the Azure
portal's diagnostics landing: it lists diagnostic **categories as cards** —
**Identity and Security** (available now), plus **Connectivity Issues**,
**Reliability**, and **Availability and Performance** as "Coming soon"
placeholders for future work. Clicking **Identity and Security** opens a focused
detail view (with a back link) that has a **My access / Dashboard identity**
toggle:

- **My access** shows the **signed-in user account** (display name, account/UPN,
  tenant id, object id), resolved from the MSAL active account and confirmed
  against `/api/me`.
- **Dashboard identity** shows the shared **managed identity** the Container App
  runs as (object id from `SHARED_IDENTITY_PRINCIPAL_ID`) — the principal that
  actually performs ARM and Storage writes, so its missing role is the usual
  root cause of a tenant onboarding failure even when the user's own access
  looks fine. In a local-dev shell with no managed identity, the view reports
  the identity as unavailable instead of a misleading empty table.

For the selected principal it lists, per resource group, the effective Azure
RBAC role assignments — direct plus those inherited from the subscription,
management groups, and (for the user) their Entra groups — each tagged `Direct`
/ `Inherited` with its scope level.

Resource groups where role enumeration fails (the caller lacks
`Microsoft.Authorization/roleAssignments/read`) are flagged as a finding rather
than silently shown as "has access". A no-role resource group is called out
explicitly. The resource groups reviewed are derived from the saved workspace
config (workload, ACR, terminal) plus sub-wide AKS cluster discovery.

The Settings **footer** now also shows the running build version
(`v<A>.<B>.<build> · <short-sha>`) to the left of the existing "Stored locally ·
`elb-prefs`" label, so the active control-plane version is visible from any
Settings tab.


## API / IaC diff summary

- **New route** `GET /api/me/access-review?subscription_id=…&resource_group=…&target=me|dashboard`
  (repeat `resource_group` to review several at once; `target` selects the
  signed-in caller or the dashboard managed identity), `require_caller`-gated,
  read-only. Returns `{ subscription_id, principal: { kind, object_id,
  available }, groups: [{ resource_group, scope, assignments: [{ role_name,
  role_guid, scope_level, inherited, assignment_scope }], degraded, reason }] }`.
- **New service** `api/services/access_review.py` — one ARM enumeration via
  `assignedTo('{oid}')` (group-inheritance aware, same semantics as the portal),
  grouped per RG with inheritance/scope-level classification and lazy custom-role
  name resolution. `review_resource_group_access` takes a `principal_oid` +
  `principal_kind` so it can review either the caller or the dashboard MI;
  `dashboard_identity_principal_id()` reads `SHARED_IDENTITY_PRINCIPAL_ID`.
  Unlike `compute_caller_permissions`, it does **not** degrade open: enumeration
  failure is reported as `degraded=true` + reason.
- **Frontend** `web/src/api/me.ts` gains `meApi.accessReview()` + the
  `AccessReviewResponse` / `AccessReviewGroup` / `AccessReviewRow` types, and
  `SettingsPanel.tsx` gains the `DiagnosticsSection` + `RgAccessCard` components
  and the "Diagnose & solve problems" nav entry.

No IaC change. No RBAC/network/auth contract change — this is a read-only
diagnostic behind the existing caller gate, so §12a hardening discipline does
not apply.

## Validation evidence

- `uv run pytest -q api/tests` → 2790 passed, 3 skipped.
  New: `api/tests/test_access_review.py` (11 tests) +
  `test_me_access_review_*` in `api/tests/test_me_route.py`.
- `uv run ruff check api/services/access_review.py api/routes/me.py …` → clean.
- `cd web && npm run build` → built in ~9 s, no type errors.
- `cd web && npm test -- --run` → 624 passed.
- `npx eslint src/components/SettingsPanel.tsx src/api/me.ts` → 0 errors
  (1 pre-existing unrelated warning at line 2144).
