# 2026-05-18 — UI/UX Phase A (16 items)

## Motivation

Round-one UX review across the eight primary menus surfaced 160 ideas
(20 per menu). Phase A picks the 16 highest-leverage items — the ones that
remove a repeated paper-cut for every researcher who uses the dashboard:
shareable URLs, visible draft state, clear blockers before submit, a
discoverable API surface, and standardised loading / degraded states.

This is the first of three planned phases. Phase B was skipped (re-validated
that those items were already implemented). Phase C will derive a further 20
items per menu without code changes.

## User-facing change

| # | Area | Change |
|---|------|--------|
| L1 | Lab Tools | Tab selection now syncs to `?tab=…`, sharable & restored on reload. |
| L2 | All pages | Reusable `<RowSkeleton>` for skeleton-row loading; wired into BLAST Jobs first. |
| D1 | Dashboard cards | (Already shipped — verified all 6 monitor cards expose `lastRefreshed`.) |
| D2 | Degraded states | Reusable `<DegradedNotice>`; surfaces canonical reason codes + recovery hints. Wired into Jobs empty state. |
| J1 | BLAST Jobs | Filter (`?filter=`) and search (`?q=`) now URL-synced via `useSearchParams`. |
| J2 | BLAST Jobs | (Already shipped — `JobsFilterBar` already renders status counts.) |
| T1 | Browser Terminal | (Already shipped — Socket/Sidecar/Shell badges already render.) |
| T2 | Browser Terminal | New `az login` health indicator + manual re-check button. Backed by new `GET /api/terminal/azure-cli` (60 s cache). |
| A1 | API Reference | New sticky left sidebar with tag list, endpoint search, and method filter chips. Two-column layout. |
| A2 | API Reference | Endpoint cards expose `#ep-METHOD-/path` deep-links and a copy-link button; URL hash auto-expands + scrolls into view. |
| C1 | Custom DB Builder | New 3-step `<WizardStepper>` (Configure → Provide FASTA → Build & publish) with state derived from the existing builder hook. |
| C2 | All pages | `formatApiError` now strips SAS / bearer / tenant-GUID leakage and produces friendlier `(ResourceNotFound)` style messages. Distinguishes timeout vs offline. |
| R1 | Job Results | (Already shipped — `StepLogSection` already renders a vertical timeline.) |
| R2 | Job Results | (Already shipped — `StorageLockedPanel` already provides one-click recovery.) |
| N1 | New Search | Draft auto-save now exposes `lastSavedAt`; footer shows "Saved Ns ago" updated every 15 s. |
| N2 | New Search | Run button is disabled with a "Resolve blockers" label whenever pre-flight returns `ready: false`. |

## API / IaC diff summary

### Backend (`api/`)

- `api/routes/terminal_ws.py`
  - **NEW** `GET /api/terminal/azure-cli` route. Requires MSAL bearer.
  - Calls `api.services.terminal_exec.run(["az", "account", "show", "-o", "json"], timeout_seconds=8)`. Returns
    `{status: "signed_in" | "signed_out" | "unknown", user?, tenant_id?, subscription_id?, hint?, error?, checked_at, cached, cache_age_s}`.
  - Process-local 60-second cache, async-lock-guarded. `?force=true` bypasses the cache.
  - Result is sanitised by `terminal_exec` (SAS / bearer / sub-id already redacted there).

No other backend files touched. The other in-flight session (BLAST tie-window
comparator + DB order oracle) is editing
`api/services/storage_data.py`, `api/services/blast_db_metadata.py` (new),
`api/services/blast_oracles.py` (new), `api/tasks/blast.py`,
`api/routes/stubs.py`, and `api/tests/test_blast_*.py`. None of those files
were modified here.

### Frontend (`web/`)

- **NEW** `web/src/components/DegradedNotice.tsx`
- **NEW** `web/src/components/RowSkeleton.tsx`
- **NEW** `web/src/pages/apiReference/ApiReferenceSidebar.tsx`
- `web/src/api/client.ts` — `formatApiError` + new `sanitiseUserFacingMessage` helper
- `web/src/pages/ApiReference.tsx` — two-column layout when spec loaded
- `web/src/pages/apiReference/EndpointCard.tsx` — deep-link / copy-anchor (A2)
- `web/src/pages/BlastJobs/BlastJobs.tsx` — adopts `<RowSkeleton>`
- `web/src/pages/BlastJobs/JobsEmptyState.tsx` — adopts `<DegradedNotice>`
- `web/src/pages/BlastJobs/useBlastJobsState.ts` — URL sync
- `web/src/pages/BlastSubmit.tsx` — passes `lastSavedAt` through
- `web/src/pages/blastSubmit/BlastSubmitFooter.tsx` — saved-ago label + pre-flight gate
- `web/src/pages/blastSubmit/useDraftForm.ts` — returns `lastSavedAt`
- `web/src/pages/DatabaseBuilder/DatabaseBuilder.tsx` — wizard stepper
- `web/src/pages/terminal/TerminalCockpit.tsx` — `az login` health item

### Infra

No infrastructure or Bicep changes.

## Validation evidence

- `uv run pytest -q api/tests` → **635 passed in 37.65 s**
- `cd web && npm run build` → **built in 8.89 s** (no errors; expected
  `chunkSizeWarningLimit` warning unchanged)
- `uv run ruff check api` → **All checks passed!**

## Security / a11y notes

- No SAS tokens issued. `azure-cli` probe relies on `terminal_exec` which is
  already EXEC_TOKEN-gated and allowlist-restricted.
- `formatApiError` now adds a defence-in-depth scrub of SAS query strings,
  `Bearer`/`SharedKey` headers, and GUID-shaped subscription/tenant ids in
  any user-facing error string. Server-side `sanitise` remains the primary
  defence; the new helper is a safety net for paths that bypass it
  (e.g. browser-native `TypeError: Failed to fetch`).
- `DegradedNotice` and `RowSkeleton` carry `role="status"` /
  `aria-live="polite"`.
- `ApiReferenceSidebar` chips use `aria-pressed`; both `<nav>` regions have
  explicit `aria-label`.
- `WizardStepper` already marks the active step with `aria-current="step"`.
- Storage `publicNetworkAccess` posture untouched.
- `ttyd` upstream (`127.0.0.1:7681`) untouched.
