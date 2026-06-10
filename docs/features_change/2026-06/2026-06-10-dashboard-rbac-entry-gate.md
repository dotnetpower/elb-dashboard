---
title: Optional dashboard RBAC entry gate
description: Add an opt-in ENFORCE_DASHBOARD_RBAC gate so a tenant member with no Azure read role on the dashboard resource group is denied at the SPA's /api/me bootstrap instead of loading the dashboard.
tags:
  - auth
  - security
---

# Optional dashboard RBAC entry gate

## Motivation

`require_caller` validates only tenant membership (signature, `aud`, `iss`,
`tid`). Any authenticated member of the configured Entra tenant could therefore
load the dashboard even with **zero** Azure RBAC on the deployment's resource
group / subscription — classic Broken Access Control (OWASP A01, the open
follow-up #1 in [security-audit-followup.md](../../copilot/security-audit-followup.md)).
A user reported being able to open the dashboard from a tenant where they hold
no role on `rg-elb-dashboard`.

## User-facing change

A new **opt-in** entry gate. When the operator sets
`ENFORCE_DASHBOARD_RBAC=true`, the SPA's identity bootstrap (`GET /api/me`)
requires the signed-in caller to hold at least a read role (Reader /
Contributor / Owner / the AKS read roles / Storage Blob Data reader) on the
platform scope (`AZURE_SUBSCRIPTION_ID` + `AZURE_RESOURCE_GROUP`). A denied
caller gets a `403 dashboard_access_denied` and the SPA renders a dedicated
**Access denied** screen (Retry / Sign out) instead of a half-broken dashboard.

Default **OFF** (Charter §12a Rule 4): with the env unset/`false` the legacy
behaviour — any tenant member loads the dashboard — is preserved exactly.

### Fail-open safety

The gate resolves the caller's roles through the shared managed identity, which
needs `Microsoft.Authorization/roleAssignments/read` at subscription scope (the
built-in `Reader` grants this). If enumeration fails (`degraded=True`), the
platform scope is unconfigured, or any unexpected error occurs, the gate
**degrades OPEN** to avoid a tenant-wide lockout. The degraded condition is
all-or-nothing (the MI can read role assignments or it cannot); it is never
selectively true for one caller, so fail-open cannot slip a no-role caller past
the gate. ARM still enforces real authorization on every data-plane action.

The `/me/permissions` and `/me/access-review` routes intentionally keep the
plain `require_caller` gate so a *blocked* caller can still inspect why they
were denied. The gate is the bootstrap/UX entry control — per-route
authorization (the full §1 design) remains future work; a knowledgeable
no-role caller can still reach read endpoints served by the MI directly.

## API / IaC diff summary

- New `api/services/dashboard_access.py` — `require_dashboard_access` dependency
  + `has_dashboard_read_access` / `is_dashboard_rbac_enforced` helpers.
- `api/routes/me.py` — `GET /api/me` now depends on `require_dashboard_access`
  (was `require_caller`). Sub-routes unchanged.
- `web/src/hooks/useDashboardAccessGate.ts` — resolves `/api/me` once, maps the
  `dashboard_access_denied` 403 to a `denied` tri-state (fail-open otherwise).
- `web/src/pages/AccessDenied.tsx` — full-screen access-denied screen.
- `web/src/App.tsx` — `AuthenticatedApp` branches on the gate
  (loading → skeleton, denied → AccessDenied, else → routes). Skipped entirely
  in `DEV_BYPASS` mode.
- `infra/modules/containerAppControl.bicep` — `ENFORCE_DASHBOARD_RBAC=false`
  on the `api` sidecar (default-OFF).

## Validation evidence

- `uv run pytest -q api/tests/test_dashboard_access.py` → 22 passed (new file).
- `uv run pytest -q api/tests` → 3160 passed, 3 skipped (full suite, persona
  matrix green).
- `uv run ruff check api` → clean.
- `cd web && npm run build` → built; `npx vitest run src/hooks src/pages` →
  464 passed.

## How to enable / test

1. Confirm the dashboard managed identity has `Reader` (or higher) at the
   subscription so it can enumerate caller roles.
2. Set `ENFORCE_DASHBOARD_RBAC=true` on the `api` sidecar.
3. Sign in as a tenant member with **no** role on `rg-elb-dashboard` → the
   Access denied screen appears. Grant that user `Reader` on the RG → after the
   ~60 s permission cache + RBAC propagation, retry succeeds.
