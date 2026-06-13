---
title: Error-surfacing audit — auto-warmup save failures now show a toast
description: A codebase-wide audit of error-swallowing paths; the one confirmed user-facing silent failure (auto-warmup preference save) now surfaces a toast.
tags:
  - ui
  - operate
---

# Error-surfacing audit + auto-warmup save toast

## Motivation

Follow-up to the "no output captured" investigation. The user asked for a
codebase-wide sweep: *any feature that errors must show the error on screen,
OpenAPI included.* I audited backend routes/tasks and the frontend for
error-swallowing paths where a failure is hidden from the UI.

## Audit outcome

The error-surfacing infrastructure is already broad and solid; most candidate
findings were false positives on verification:

- `api/routes/blast/jobs.py` — `external_degraded` IS merged unconditionally
  (not gated on a non-empty job list), so an unreachable OpenAPI plane is
  surfaced even with zero local jobs.
- `api/routes/blast/preflight.py` — each check's `except` appends an explicit
  `status="fail"` row (broker, ACR, cluster, …); failures are not swallowed.
- OpenAPI "Try it" `ResponseViewer` renders status `0` as a red **Error** panel
  with the error body — network/sibling failures are visible.
- `SetupWizard` resource-creation mutations render `mutation.isError` in
  `ResourceRow`/`RgField`; `PlsTransitionBanner` renders `recreate.error` via
  `formatApiError`.
- Celery `warmup` / `prepare_db` / submit tasks write `status="failed"` with the
  error on failure; `databases_shard` writes `sharding_error` (and has stale-flag
  recovery), which the SPA renders.
- Monitor routes degrade through `_graceful` with a machine+human
  `degraded_reason`/`degraded_message`, consumed by the cards and
  `useBlastJobsState`.

## The one confirmed gap (fixed)

`web/src/components/ClusterItem/ClusterItem.tsx` synced the user's auto-warmup
database selection with a fire-and-forget `.catch(() => {})`. A backend failure
left the toggle looking persisted while nothing was saved, with no error shown.

## User-facing change

Auto-warmup preference save failures now raise a single error toast
(`Auto-warmup preference not saved: <reason>`), deduplicated per distinct failed
payload via an error-key ref so a dependency-driven re-run of the sync effect
does not spam, while still allowing a retry on the next change.

## API / IaC diff summary

- `web/src/components/ClusterItem/ClusterItem.tsx`: add `useToast` +
  `formatApiError`; the auto-warmup save `.then/.catch` now clears an error
  latch on success and toasts once per failed payload. No backend/API change.

## Validation evidence

- `cd web && npx tsc --noEmit` → clean.
- `cd web && npm run build` → built OK.
- `cd web && npm test -- --run` → 96 files, 859 tests passed.
