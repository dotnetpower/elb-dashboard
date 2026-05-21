# 2026-05-21 — BLAST live pod log discovery resilient to lazy state writes

## Motivation

Users running BLAST jobs reported that only `azcopy` style logs appeared in the
dashboard while the run was active, even though `kubectl logs` showed rich
output (`BLAST RUNTIME`, command lines, results-export upload) on the real
`blastn-batch-*` pods. Empirical inspection of a recent job
(`18b9edca-4d8a-41bc-9cd8-3a2f161259b6`) confirmed that the live SSE stream
discovered **0** Kubernetes pod targets even though 21 matching pods existed
in the cluster.

Root cause: the SSE handler in [api/routes/blast/logs.py](../../../api/routes/blast/logs.py)
only read the top-level `payload.elastic_blast_job_id` field when seeding
the live follow loop. That field is populated by the submit task and the
background reconcile task — but several state-write paths (notably the
periodic refresh and synthesised completion) update only the nested
`_progress.steps.running.k8s.job_id` / `external.k8s.job_id` mirrors. Jobs
whose row was last touched by those paths thus presented `elastic_blast_job_id`
= `None` at the top level, the suffix fallback in
[`discover_k8s_log_targets`](../../../api/services/job_logs/k8s.py)
matched the dashboard UUID (no overlap with the `job-<hash>` pod suffix), and
nothing was discovered.

## User-facing change

Live SSE pod log discovery now finds the right ElasticBLAST job id regardless
of which state-write path most recently touched the row. The "Live Stream"
panel under the *Running* step shows real `blastn` / `results-export` /
`init-ssd` container output again, instead of only the submit task's azcopy
lines.

## API / IaC diff summary

- New helper `api.services.job_logs.k8s.resolve_elastic_blast_job_id(payload)`
  walks every known mirror (`elastic_blast_job_id`, `k8s_job_id`,
  `_progress.steps.{running,exporting_results,warming_up,staging_db}.k8s.job_id`,
  `external.k8s.job_id`) and returns the first `job-<hash>` it finds.
- [api/routes/blast/logs.py](../../../api/routes/blast/logs.py) imports and
  uses the helper in `k8s_follow_manager` instead of the bare top-level
  lookup.
- No Bicep / IaC changes. No new dependency.

## Validation

- `uv run pytest -q api/tests/test_job_log_k8s.py api/tests/test_blast_results_routes.py`
  → 39 passed (4 new resolver tests + existing pod discovery / streaming
  tests).
- `uv run pytest -q api/tests/test_blast_tasks.py api/tests/test_route_contracts.py api/tests/test_job_log_event_bus.py`
  → 121 passed (no regressions in submit task, route registration, or
  pub/sub flow).
- `uv run ruff check api` → clean.
- Empirical: a Python REPL against the live `b052302c-…` / `rg-elb-01` /
  `elb-cluster` environment with the broken-before job
  `18b9edca-4d8a-41bc-9cd8-3a2f161259b6`:
  - Before fix: `payload.elastic_blast_job_id` is `None` → 0 discovered
    targets.
  - After fix: resolver returns `job-429825360482416da20736cf3ed51a95`,
    `discover_k8s_log_targets(...)` returns 21 targets covering
    `blastn-batch-s{NN}-job-000-…` (`blast`, `results-export`),
    `init-ssd-…`, and `elb-finalizer-…` pods.

## Out of scope (separate decision needed)

The Kubernetes pod log content itself is **still only delivered live over
SSE**. Once the SSE client disconnects or the job reaches a terminal phase,
the real `blast` / `results-export` / `init-ssd` container output is not
written to any durable storage — only `staging_db.last_output` (the submit
task's azcopy stdout) survives. This is the broader half of the user's
"azcopy 정도만 나오는데" complaint and would require a new write path in
the artifact finalizer (`api/tasks/blast_artifacts.py`) or an equivalent
celery task to fetch a tail of each completed pod's log and persist it to
`steps.{running,exporting_results}.last_output` / a new
`execution-steps/logs/*` chunk. Tracked separately.
