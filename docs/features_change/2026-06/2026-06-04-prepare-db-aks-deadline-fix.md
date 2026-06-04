# prepare-db AKS: raise Job deadline + per-task Celery limits so `nt`/`core_nt` finish

## Motivation

A live `nt` prepare-db run via AKS reported **"Partial copy · 1081 failed"** even
though no shard actually errored. Tracing the failure through code + Log Analytics
proved a timeout mismatch, not a data error:

- The Indexed K8s Job was created with `activeDeadlineSeconds = 2700` (45 min).
- `nt` (~4.8k files, the UI badges it "May take hours") was still streaming from
  NCBI at the 45-min mark. K8s enforced the deadline and marked the Job
  `Failed / DeadlineExceeded`, abandoning every still-in-flight and not-yet-started
  shard.
- The Celery poller observed the terminal `Failed` condition and ran the per-blob
  reconcile. The 1081 files that had not yet been committed (4814 − 3733) were
  counted as `failed: missing` — surfacing as a misleading "partial · 1081 failed".

Evidence (moonchoi prod, Log Analytics `ContainerAppConsoleLogs_CL`):
`nt` start `08:40:38`; `08:40:38 + 2700s = 09:25:38` Job killed; reconcile finished
`done elapsed=3200.2s` at `09:33:58`. The worker itself succeeded in 3201s — it was
**not** a Celery time-limit kill (3200 < the 3600 global hard limit), confirming the
root cause was the 45-min Job deadline, not the task limit *this* run.

A secondary latent defect: had the Job deadline simply been raised, the global
Celery hard limit (1h, `api/celery_app.py`) would have SIGKILLed the poller on the
*next* multi-hour run, orphaning the Job and stranding the DB at `partial`.

## User-facing change

- `nt` / `core_nt` prepare-db via AKS now runs to genuine completion instead of
  being cut off at 45 min and falsely reported as a partial failure with ~1000
  "missing" files.
- No change to small/medium DBs: the Job still exits the instant all shards
  succeed, so a larger ceiling never slows a quick run.

## API / IaC diff summary

- `api/services/k8s/prepare_db_jobs.py`: `DEFAULT_ACTIVE_DEADLINE_SECONDS`
  `2700` → `4 * 60 * 60` (14400). Still overridable per-job via the route env
  `PREPARE_DB_AKS_JOB_TIMEOUT_SECONDS`.
- `api/routes/storage/prepare_db.py`: the env default and the `ValueError`
  fallback for `PREPARE_DB_AKS_JOB_TIMEOUT_SECONDS` both move `2700` → `14400`.
- `api/tasks/storage/prepare_db_via_aks.py`:
  - `_JOB_POLL_MAX_SECONDS` `4h` → env-driven default `4h15m` (15300) so the
    poller always outlives the Job deadline and observes the terminal condition.
  - New `_TASK_SOFT_TIME_LIMIT` / `_TASK_HARD_TIME_LIMIT` (env-driven, default
    poll-cap + 20 min / + 30 min), wired into the `@shared_task` decorator as
    `soft_time_limit` / `time_limit` so this task overrides the global 1h worker
    limit. A startup assertion enforces `soft < hard` and `hard > poll-cap`.
- No Bicep / infra change. The three new env vars
  (`PREPARE_DB_AKS_JOB_POLL_MAX_SECONDS`, `PREPARE_DB_AKS_TASK_SOFT_TIME_LIMIT`,
  `PREPARE_DB_AKS_TASK_TIME_LIMIT`) are optional overrides with safe defaults.

Timeout ladder (all overridable, defaults shown):

| Layer | Value | Why |
| --- | --- | --- |
| Job `activeDeadlineSeconds` | 14400 (4h) | K8s caps the whole Job |
| Celery `_JOB_POLL_MAX_SECONDS` | 15300 (4h15m) | poller outlives the Job |
| Celery `soft_time_limit` | 16500 (4h35m) | margin for post-job reconcile sweep |
| Celery `time_limit` (hard) | 17100 (4h45m) | final backstop, > global 1h |

## Deploy note

This is `api/` code, so it takes effect only after the `worker` (and `api`)
sidecars are redeployed. A deploy-free interim mitigation is to set the Container
App env `PREPARE_DB_AKS_JOB_TIMEOUT_SECONDS=14400` together with raised
`CELERY_TASK_TIME_LIMIT` / `CELERY_TASK_SOFT_TIME_LIMIT` — though an env change
also rolls a new revision.

> **Override coupling:** the Job deadline (`PREPARE_DB_AKS_JOB_TIMEOUT_SECONDS`,
> read in the route) and the poller ceiling (`PREPARE_DB_AKS_JOB_POLL_MAX_SECONDS`,
> read in the worker) are independent envs. If you raise the Job deadline beyond
> the poll ceiling without also raising the ceiling, the poller will declare its
> own `timed_out` *before* the Job finishes — degrading back to a premature
> "partial" (the Job still completes idempotently in K8s, so it is recoverable,
> not data loss). Raise both together, and keep
> `PREPARE_DB_AKS_TASK_TIME_LIMIT` above the poll ceiling.

## Future work (out of scope here)

- `_poll_copy_completion` accounts uncommitted-but-in-progress blobs as
  `failed: missing`; the resume/retry path already re-runs them idempotently, but
  the wording is misleading. A "still copying" vs "genuinely missing" distinction
  would improve the partial message.
- Per-index resilience: `backoffLimit` is Job-wide (capped `<= 5` by an existing
  test). `backoffLimitPerIndex` would isolate a single persistently-bad file from
  dooming an otherwise-complete large Job.

## Validation

- `uv run pytest -q api/tests/test_prepare_db_aks_manifest.py api/tests/test_prepare_db_aks_task.py api/tests/test_prepare_db_aks_route.py` → 52 passed.
- New regression guard `test_task_time_limits_outlive_job_poll_and_deadline`
  asserts the full ladder (job deadline ≤ poll cap < soft < hard, hard > 3600).
- Renamed `test_manifest_default_active_deadline_is_4_hours` asserts the 14400
  default in both the constant and the rendered manifest.
- `uv run pytest -q api/tests` (full suite) + `uv run ruff check api`.
