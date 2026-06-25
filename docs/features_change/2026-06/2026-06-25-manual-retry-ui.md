---
title: Manual retry + auto-retry status for failed jobs
description: A one-click Retry button and failure-classification / quarantine badges on the BLAST job detail page, completing the auto-retry feature so the gate can be flipped on with a UI to observe and override it.
tags:
  - user-guide
  - blast
---

# Manual retry + auto-retry status for failed jobs

## Motivation

The auto-retry engine shipped backend-only and default-OFF — there was no UI to
observe what it would do or to override it, so the gate could not responsibly be
turned on. This adds the **operator-facing** half: a one-click manual retry and
status badges that read the `failure_classification` / `auto_retry` fields the job
projection already emits.

## User-facing change

* The BLAST job detail header now shows a **Retry** button on a failed job whose
  failure is a transient submit-phase infrastructure error
  (`failure_classification.auto_retryable`) and whose submit parameters are
  reconstructable. One click re-submits with the original parameters (the query
  is reused from the stored `query_file`).
* An **auto-retry status badge** shows `Auto-retry N/max` or `Quarantined` on a
  failed job the auto-retry sweep has touched.
* Runtime / configuration / cluster-state failures do **not** get a one-click
  Retry — they are steered to the existing Duplicate flow (review + re-enter),
  because a blind resubmit would orphan a cluster job or repeat a deterministic
  failure.

## Design

* New route `POST /api/blast/jobs/{job_id}/retry` (in `jobs_lifecycle.py`,
  mirroring the cancel route): `require_caller` + `_assert_job_owner`, rejects
  non-failed (409), external (400), non-`auto_retryable` (400, with the category),
  and unrestorable (400) jobs. It reuses `restore_submit_kwargs` and
  `_safe_delay(submit, …)`, with **enqueue-before-flip** so a broker outage leaves
  the job in its terminal `failed` state.
* A manual retry **resets** the `auto_retry` counter + quarantine (the user is
  explicitly starting over) and drops the stale `_progress` timeline so the
  resubmitted task rebuilds it. Records a `manual_retry` history event.
* Frontend: `BlastJobSummary` gains optional `failure_classification` +
  `auto_retry` fields; `blastApi.retryJob`; a `retryMutation` in
  `useBlastResultActions`; the Retry button + badge in `BlastJobHeader`.

### Hardening (post-critique)

* The Retry button is gated on a reconstructable `query_file` so a click cannot
  fail with a 400 — unrestorable jobs simply do not show the button.
* The auto-retry badge only renders on a failed job (keyed on
  `failure_classification` presence), so a job that *succeeded after* a retry does
  not carry a stale "Auto-retry 1/2" badge.

## Enables

With this UI in place the `BLAST_AUTO_RETRY_ENABLED` gate can be flipped on after
a dogfood soak: operators can now see what was auto-retried/quarantined and
manually override.

## Validation evidence

* `uv run pytest -q api/tests/test_blast_manual_retry.py api/tests/test_route_contracts.py` — 11 passed (transient OK, not-found 404, not-failed 409, runtime 400, unrestorable 400, external 400, + route auth contract).
* `uv run ruff check api` — all checks passed.
* `cd web && npm run build` — built successfully.
* `uv run pytest -q api/tests` — 4627 passed, 3 skipped, 1 failed. The single
  failure (`test_control_plane_env.py::test_bicep_references_every_guard_key`,
  `STORAGE_DATE_LAYOUT_ENABLED`) is pre-existing and unrelated — this change
  touches no `infra/` file.
