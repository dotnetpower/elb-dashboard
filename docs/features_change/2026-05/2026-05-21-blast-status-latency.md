# 2026-05-21 — BLAST status latency: list refresh + per-phase throttle + per-job poller (+ sibling repo parallelism)

## Motivation

A BLAST submit measured against job `10949573-3997-4e96-9bfb-d6b8f61c20c5` showed
~4 m 7 s of perceived wall-time on the dashboard while the K8s job itself ran in
21 s (7 s container compute). The ~35× perceived slowdown came from four
independent latency sources:

1. The `GET /api/blast/jobs` list endpoint did **not** refresh active rows
   against K8s — it only returned whatever was last persisted. The detail
   endpoint refreshed, but the dashboard's primary view is the list. A
   finished job stayed `running` in the list until the next 60 s beat tick
   of `blast-reconcile-stale-jobs` flipped it.
2. The per-job K8s refresh throttle inside
   `_refresh_running_blast_state` was a flat 20 s. That makes sense for
   `submitted` (waiting for a long pull/init) but is too coarse for
   `running`/`results_pending`, where a 5 s probe is appropriate.
3. After a successful `submit`, there was no per-job poller — we relied
   entirely on the beat tick (up to 60 s old) and on the user manually
   opening the detail page. That alone could account for 30-50 s of the
   observed latency on a fast job.
4. In `dotnetpower/elastic-blast-azure`, the warm-cluster reuse path inside
   `ElasticBlastAzure._initialize_cluster` ran three independent operations
   sequentially (`_cleanup_stale_jobs`, `kubernetes.create_scripts_configmap`,
   `_upload_queries_only`). On a warm cluster these are the only meaningful
   bootstrap steps before the K8s Job is created — running them in parallel
   cuts the perceived submit-CLI cost.

## User-facing change

A BLAST job that finishes on K8s now flips to `completed` on the dashboard's
job list within **~10 s** instead of waiting up to 60 s for the next beat
reconcile. Three back-end mechanisms cooperate:

- The list endpoint refreshes each active row before responding (uses the
  shared per-job throttle so the dashboard polling cadence cannot stampede
  K8s).
- The refresh throttle is now 5 s for `running` / `results_pending` and
  20 s for `submitted` (the longest phase, where K8s state changes slowly).
- A new Celery task `api.tasks.blast.poll_running_status` is enqueued at
  the end of `submit` with a 10 s start delay and self-reschedules every
  10 s while the row is still active, up to 180 iterations (~30 minutes).
  The 60 s beat reconcile remains the safety net.

The sibling repo (`dotnetpower/elastic-blast-azure`) parallelises the warm-
cluster reuse shortcut by default, with an opt-out env var
(`ELB_PARALLEL_WARM_REUSE=0`).

## API / IaC diff summary

### `elb-dashboard`

- `api/services/blast_job_state.py`
  - New constants `_K8S_REFRESH_FAST_INTERVAL_SECONDS = 5.0`,
    `_K8S_REFRESH_FAST_PHASES = {"running","results_pending"}`, helper
    `_refresh_min_interval_seconds(phase)`.
  - `_refresh_running_blast_state` now reads scope (`subscription_id`,
    `resource_group`, `cluster_name`, `storage_account`) from top-level
    columns first, so callers can pass rows obtained with
    `list_for_owner(..., include_payload=False)`. Before any `repo.update`,
    it reloads the full payload via `_maybe_reload_with_payload` so the
    merged `_progress` carries existing step history.
- `api/routes/blast/jobs.py`
  - `blast_jobs_list` iterates rows whose phase is in `_K8S_REFRESH_PHASES`
    and calls `_refresh_running_blast_state(repo, row)` (debug-logged on
    failure, never 500s).
- `api/tasks/blast/__init__.py`
  - New `poll_running_status` shared task (`name="api.tasks.blast.poll_running_status"`,
    queue `blast`). Self-reschedules with `countdown=POLL_RUNNING_INTERVAL=10`
    while status ∈ {`running`,`pending`,`queued`} and phase ∈
    `_K8S_REFRESH_PHASES`. Capped at `POLL_RUNNING_MAX_ITERATIONS=180`.
  - `submit` enqueues the poller in its success branch (only when
    `status == "running"` and `phase ∈ _POLL_RUNNING_ELIGIBLE_PHASES`).
- `api/tests/test_local_to_blast_job.py`: +2 tests
  (`*_running_phase_uses_short_throttle`, `*_reads_top_level_columns`).
- `api/tests/test_blast_tasks.py`: +4 tests covering missing-row,
  terminal-status, reschedule-on-active, and max-iterations stop.
- `api/tests/test_external_blast_api.py`: +1 test
  (`test_canonical_jobs_list_refreshes_active_local_rows`).

No IaC changes. No new Celery beat schedule (only a per-job apply_async).
No new env vars on the dashboard side.

### `dotnetpower/elastic-blast-azure` (sibling)

- `src/elastic_blast/azure.py` `_initialize_cluster` warm-reuse branch:
  wraps `_cleanup_stale_jobs`, `create_scripts_configmap`, and
  `_upload_queries_only` in a `ThreadPoolExecutor(max_workers=3,
  thread_name_prefix='elb-warm-reuse')`. Opt-out via
  `ELB_PARALLEL_WARM_REUSE=0`.

## Validation evidence

```bash
$ cd /home/moonchoi/dev/elb-dashboard
$ uv run pytest -q api/tests
853 passed in 43.81s

$ uv run ruff check api
All checks passed!

$ cd web && npm run build
✓ built in 6.95s

$ cd /home/moonchoi/dev/elastic-blast-azure
$ pytest -q tests/azure/test_warm_cluster.py
17 passed in 9.83s
```

Targeted runs of the new tests:

- `test_refresh_running_blast_state_running_phase_uses_short_throttle` — passes
- `test_refresh_running_blast_state_reads_top_level_columns` — passes
- `test_poll_running_status_returns_missing_when_row_absent` — passes
- `test_poll_running_status_returns_without_reschedule_on_terminal_status` — passes
- `test_poll_running_status_reschedules_when_still_active` — passes
- `test_poll_running_status_stops_at_max_iterations` — passes
- `test_canonical_jobs_list_refreshes_active_local_rows` — passes

Manual rollout note: the sibling repo change ships in the
[elastic-blast-azure](https://github.com/dotnetpower/elastic-blast-azure)
image tag bump — `IMAGE_TAGS` in `api/services/image_tags.py` was **not**
bumped here because the sibling commit hasn't shipped yet. Update
`IMAGE_TAGS["elastic_blast"]` once the sibling commit is published, in a
follow-up PR.
