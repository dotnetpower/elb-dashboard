# E2E: restricted-user (Reader) persona + core_nt mock-lane permissions

## Motivation

The user asked whether a **limited-permission operator** (a `rb-elb-dashboard`
Reader, configured as `user1`) can be exercised in E2E, and to enrich the
core_nt scenarios. Until now the mock E2E lanes had **no `/api/me/permissions`
mock at all**, so `usePermissions` fell through to its fail-open default and
every mutating control rendered enabled — the restricted-user UX was entirely
uncovered.

## What was added

1. **Permissions mock infrastructure** (`scripts/e2e/fixtures/mockApi.ts`):
   - `UiMockState.permissions` + `setPermissions(partial)` — a per-test override
     applied *before* navigation so `usePermissions` reads it on first fetch.
   - `FULL_PERMISSIONS` (Owner-like, default) and `READER_PERMISSIONS`
     (subscription Reader + Storage Blob Data Reader; every `can_*` false,
     `degraded:false`). `degraded:false` is essential — the SPA only disables a
     control when the capability is false AND not degraded.
   - A `**/api/me/permissions**` route serving `state.permissions`.
   - Four graceful read-only mocks (`/api/aks/skus`, `/api/warmup/auto-preference`,
     `/api/monitor/aks/top-nodes`, `/api/monitor/aks/start-stats`) the cluster
     row polls, so the ui-mock lane stays hermetic.

2. **New scenario** `scripts/e2e/scenarios/restricted-user-events.ui.spec.ts`
   (3 cases, all green):
   - Reader sees **Stop** and **Delete** disabled on the cluster row, with the
     permission-denied tooltip ("You hold: Reader … You need: …").
   - Reader sees **Run BLAST** disabled on New Search, tooltip = "do not have
     permission to submit BLAST jobs".
   - Control case: the default full-access caller keeps Stop/Delete enabled
     (guards against a false-positive where buttons are disabled for an
     unrelated reason).

This exercises the same capability contract the api sidecar computes in
`api/services/me_permissions.py` (Reader role → no write/start-stop/delete/
submit), mocked at the `/api/me/permissions` boundary so no real restricted
Azure principal is needed for the mock lane.

## How I can run E2E myself (no Azure cost)

The `ui-mock` + `mutation-mock` lanes intercept `/api/*` in the browser. I run
them by starting vite on :8090 (+ a dev-bypass api on :8085 with
`CORS_ALLOW_ORIGINS=http://localhost:8090` so unmocked polling endpoints degrade
gracefully instead of CORS-failing), then:

```bash
npx playwright test -c web/playwright.e2e.config.ts --project ui-mock --project mutation-mock
```

The sanctioned wrapper does the same setup automatically:

```bash
scripts/dev/e2e-ui.sh bypass --headless -- npm --prefix web run e2e:all-safe
```

## Real restricted-user (login mode) — follow-up, needs Azure

The mock lane proves the **gating UX**. To prove the **real RBAC enforcement**
end-to-end (a true Reader principal getting 403 from ARM/Storage at submit), the
existing `api-blast-submit-smoke` / `azure-core-nt-lifecycle` lanes accept
`E2E_BEARER_TOKEN`. Plan: acquire a bearer token for the `user1` Reader
principal (`az account get-access-token` while signed in as user1, audience =
the api app registration) and pass `E2E_BEARER_TOKEN=<token>` in login mode to
assert the api returns 403 on `/api/aks/stop` and `/api/blast/jobs`. This is the
natural next core_nt enrichment but requires the user1 credential + a running
fullstack, so it is documented here rather than executed.

## Validation evidence

- `npx playwright test --project ui-mock --project mutation-mock` — **17 passed**
  (14 existing + 3 new restricted-user cases).
- `npx playwright test restricted-user-events` — 3 passed in isolation.
- `npx vitest run src/pages/blastSubmit src/api/upgrade.test.ts` — 200 passed
  (no unit regression).
- No product code changed — additive test fixtures + one new scenario file.
