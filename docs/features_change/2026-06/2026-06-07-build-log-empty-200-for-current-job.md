---
title: Upgrade build-log endpoint returns empty 200 for the in-flight job
description: A not-yet-built component's missing build-log blob now returns an empty 200 for the current upgrade job instead of a 404 the SPA polls every 3 s, removing ~2k daily failed-request rows and per-poll browser console errors.
tags:
  - operate
  - release
---

# Upgrade build-log endpoint returns empty 200 for the in-flight job (2026-06-07)

## Motivation

The Self-upgrade page renders three `BuildLogViewer`s (api / frontend /
terminal) and each polls
`GET /api/upgrade/jobs/{job_id}/build-log/{component}` every 3 s while
the upgrade is active. The per-component Append Blob is only created when
that component's `az acr build` actually starts, so during the api build
the frontend and terminal logs do not exist yet and the endpoint returned
`404 build log not found` on every poll.

Live App Insights showed this path as the single largest source of failed
requests: **2,068 × 404 in 24 h** for
`GET /api/upgrade/jobs/{job_id}/build-log/{component}`, plus a browser
console error on every poll. The SPA already treats that 404 as a benign
"No log yet for this component", so the 404 carried no information — it was
pure telemetry and console noise.

## User-facing change

While an upgrade is mid-flight, a not-yet-built component's log now
returns an empty `200` (the viewer keeps showing `(empty)` exactly as
before) instead of a `404`. No more per-poll browser console errors and no
more 404 flood in App Insights. A genuinely unknown `job_id` (e.g. a job
whose logs were pruned) still returns `404`.

## API / IaC diff summary

- `api/routes/upgrade.py` — `upgrade_build_log`: on `KeyError` (blob
  missing) it now reads `state.get_state().job_id` and, when the requested
  `job_id` equals the current upgrade job, returns an empty
  `200 text/plain` response. Any other `job_id` still raises `404`. The
  state read happens only on the blob-missing path, so the happy path
  (blob present) is unchanged. `400` for an invalid `job_id` / component is
  unchanged, and the route's `require_upgrade_admin` gate is unchanged.
- No IaC change.

## Validation evidence

- `uv run pytest -q api/tests/test_upgrade_routes.py api/tests/test_persona_matrix.py` — 85 passed, including the new
  `test_build_log_endpoint_empty_200_for_current_job_not_yet_built`
  (current job → empty 200; unknown job → 404) and the unchanged
  `test_build_log_endpoint_404_for_missing` / `_requires_admin` /
  `_rejects_invalid_component`.
- `uv run ruff check api/routes/upgrade.py api/tests/test_upgrade_routes.py` — clean.
- Live diagnosis: App Insights 24 h 4xx-by-path showed
  `GET /api/upgrade/jobs/{job_id}/build-log/{component}` 404 × 2068 (the
  symptom this fix removes).
