---
title: In-app job notification center
description: Header bell that surfaces terminal BLAST jobs (completed/failed/cancelled) with an unread badge, derived from jobstate with a per-user seen marker.
tags:
  - user-guide
  - blast
---

# In-app job notification center

## Motivation

The operations-readiness checklist (section 4, "job lifecycle automation") calls
out the core pain: a researcher must keep watching the dashboard to know when a
job finishes. There was no notification surface — job completion/failure was only
visible by polling the job list. This adds the first, lightest notification
channel: an in-app bell with an unread badge.

## User-facing change

* A bell icon now sits in the top header, left of the Settings gear.
* It shows an unread count badge for terminal BLAST jobs
  (`completed` / `failed` / `cancelled`) that finished since the user last
  looked.
* Clicking it opens a dropdown feed of recent terminal jobs with status, program,
  database, relative time, and (for failures) the error code. "Mark all read"
  clears the badge.
* The feed polls every 30 s and refetches on window focus.

## Design — derived view, no new write path

The notification feed is a **derived view** over the existing `jobstate` table,
not a stored event stream:

* "Notifications" = the caller's most-recent terminal, non-child jobs from
  `JobStateRepository.list_for_owner` (split children — rows with a
  `parent_job_id` — are excluded so a fan-out job yields one notification, not N).
* "Unread" = a job whose `updated_at` is newer than a single per-user
  `last_seen_at` marker. This is safe because terminal jobstate rows are not
  re-written (`_update_state` no-op shortcut + reconcile skips terminal rows +
  finalizers do not bump the row), so `updated_at` is a stable "became terminal
  at" anchor.

This avoids a new notifications table, a terminal-transition write hook, and the
"which of the three terminal entry points do I hook?" problem entirely — the feed
is computed at read time regardless of which path (submit/poll, OpenAPI webhook,
or reconcile) drove the job to terminal.

The first read of a brand-new user **seeds the marker to "now"** so they start at
zero unread instead of a flood of historical completions; only jobs that finish
after that first read then count as unread. This is a deliberate (idempotent,
once-per-user) write side effect on `GET`.

Cluster-shared rows (`owner_oid=""`, from external OpenAPI sync) are included in
`list_for_owner` and therefore can appear in the feed — acceptable under the
single-tenant operator model.

## API / IaC diff summary

New backend:

* `api/services/notifications.py` — derived feed + `notifseen` Azure Table marker
  (read/write best-effort, never raises; follows the autostop table pattern).
* `api/routes/notifications.py` — `GET /api/notifications` (feed + unread count)
  and `POST /api/notifications/seen` (advance marker). Both `require_caller`.
* `api/main.py` — router registered above the frontend catch-all.

New frontend:

* `web/src/api/notifications.ts` — typed client (+ barrel export in `endpoints.ts`).
* `web/src/hooks/useNotifications.ts` — TanStack Query polling + mark-seen mutation.
* `web/src/components/NotificationBell.tsx` — header bell + badge + dropdown.
* `web/src/components/Layout.tsx` — bell wired into the header (`enabled={!!account}`).

No IaC change: the marker table is created on first use via
`create_table_if_not_exists`, like the existing `autostop` table. No new env var,
no Bicep change, no new Azure resource, no SAS token.

## Validation evidence

* `uv run pytest -q api/tests/test_notifications.py` — 9 passed (terminal/child
  filtering, unread accounting, first-read seeding, no-seed path, mark-seen,
  listing-failure degrade, marker-read-failure degrade, both routes).
* `uv run ruff check api` — all checks passed.
* `cd web && npm run build` — built successfully, no type errors.
* `uv run pytest -q api/tests` — 4537 passed, 3 skipped, 1 failed. The single
  failure (`test_control_plane_env.py::test_bicep_references_every_guard_key`,
  re: `STORAGE_DATE_LAYOUT_ENABLED` worker/beat references) is pre-existing and
  unrelated — this change touches no `infra/` or `control-plane-env.json` file.
