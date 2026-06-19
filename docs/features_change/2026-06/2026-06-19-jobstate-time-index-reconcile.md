---
title: Periodic jobstate time-index reconcile (heals best-effort index gaps)
description: A flag-gated Celery beat task re-runs the idempotent jobstate time-ordered index backfill on a schedule, so a job whose in-line best-effort index write failed is re-added and stops being silently omitted from the indexed /api/blast/jobs listing — closing the partial-failure gap that gated flipping JOBSTATE_TIME_INDEX_ENABLED on.
tags:
  - blast
  - architecture
---

# Periodic jobstate time-index reconcile (heals best-effort index gaps)

## Motivation

The jobstate time-ordered index (#50, shipped flag-OFF in `b4142db`) maintains
`jobstateindex` with **best-effort** writes: `_index_put` / `_index_delete` are
wrapped in try/except so an index write failure never fails the source-of-truth
`jobstate` row. The documented residual gap: if `_index_put` fails *after* the
row write (a transient Table error) while `JOBSTATE_TIME_INDEX_ENABLED` is on,
that one job is silently omitted from the indexed `/api/blast/jobs` listing until
a backfill re-runs. Both the issue and the repo design notes flagged a **periodic
reconcile** as the hardening prerequisite before trusting the flag long-term.

## User-facing change

No user-visible behaviour change while the flag is OFF (the default): the task
returns early before touching Storage. Once `JOBSTATE_TIME_INDEX_ENABLED=true`,
the listing self-heals — a job missed by a transient index-write failure is
re-added on the next reconcile tick instead of staying invisible until a manual
backfill.

## API / IaC diff summary

- `api/services/state/repository.py` — new `JobStateRepository.reconcile_time_index(*, dry_run=False, batch_log_every=500) -> (scanned, written)`: the idempotent scan-and-upsert (read-only against `jobstate`, upsert-only against `jobstateindex`) extracted so the one-shot backfill and the periodic reconcile share one implementation and can never drift. Upsert-only is sufficient: a failed `_index_delete` leaves a stale index row for a soft-deleted job, but `list_owner_page` already skips `status='deleted'`/missing rows at read time, so the reconcile only needs to *add* missed rows (and never re-adds tombstones via the `status ne 'deleted'` filter).
- `scripts/dev/backfill_jobstate_time_index.py` — `backfill()` is now a thin CLI wrapper over `reconcile_time_index()`; the stdout summary (`{mode}done scanned=<n> backfilled=<n>`) and idempotency contract are unchanged.
- `api/tasks/blast/time_index_reconcile_task.py` — new `@shared_task` `api.tasks.blast.reconcile_time_index`. No-op (returns `{"skipped": "flag_off", ...}`) unless `JOBSTATE_TIME_INDEX_ENABLED` is set; otherwise calls `repo.reconcile_time_index()` and returns `{scanned, written}`.
- `api/tasks/blast/__init__.py` — register the new task for Celery discovery.
- `api/celery_app.py` — beat entry `blast-reconcile-time-index` on the `reconcile` queue, `CELERY_BEAT_TIME_INDEX_RECONCILE_SECONDS` (default 3600 s). Free to leave scheduled on every deployment — one cheap env check per tick when the flag is off (same pattern as the Service Bus beat tasks).

Charter §12a Rule 4 compliant: new behaviour ships additive and default-OFF
behind the existing `JOBSTATE_TIME_INDEX_ENABLED` flag.

## Validation evidence

- `uv run pytest -q api/tests/test_jobstate_time_index.py` — 19 passed (16 prior + 3 new): repo-method dry-run touches no table; task no-ops when the flag is off (no index table created); task heals un-indexed rows when the flag is on, skips tombstones, and is idempotent on a second pass.
- `uv run pytest -q api/tests/test_blast_tasks.py api/tests/test_celery_failure_visibility.py` — 167 passed (task package + Celery wiring unaffected).
- `uv run pytest -q api/tests/test_state_repo.py api/tests/test_blast_jobs_routes.py api/tests/test_persona_matrix.py api/tests/test_smoke.py api/tests/test_state_singletons.py` — 206 passed.
- `uv run ruff check` — clean on all touched files.
- Registration verified: `api.tasks.blast.reconcile_time_index` registers on the Celery app (via the existing `include=["api.tasks.blast", …]`) and the `blast-reconcile-time-index` beat entry resolves to the `reconcile` queue.

## Remaining (#50, still open)

- Flip after a verified in-cluster backfill (maintainer/ops action) — now de-risked by this reconcile.
- Route/SPA `next_cursor` wiring (complicated by the external `/v1/jobs` merge in `_compute_blast_jobs_response`).
- `list_for_scope` / `list_all` index buckets (operator/dev surfaces) stay on the legacy scan.
