# BLAST Execution Scenario Matrix

## Submit Surfaces

Dashboard submit path:

- Endpoint: `POST /api/blast/jobs` with dashboard payload fields such as `query_data`, `aks_cluster_name`, `db`, `storage_account`, `cluster_name`, and sharding options.
- Expected route: local FastAPI validation, JobState row creation, Celery task enqueue on the `blast` queue, worker progress checkpoints, events at `/api/blast/jobs/{job_id}/events`, queue state at `/api/blast/jobs/{job_id}/queue`.
- Key assertions: HTTP 202, `admission.decision=accepted`, `Retry-After`, stable `job_id`, persisted `task_id`, job visible in `/api/blast/jobs`, no stuck `queued` row after worker failure cleanup.

OpenAPI submit path:

- Endpoint: `POST /api/v1/elastic-blast/submit` with `query_fasta`, `db`, `program`, optional taxonomy fields, and `options.outfmt=5`.
- Canonical endpoint compatibility: `POST /api/blast/jobs` with inline `query_fasta` delegates to the external OpenAPI execution plane.
- Expected route: FastAPI validation, trusted `submission_source=external_api`, sibling submit through `api.services.external_blast`, public status vocabulary `queued | running | success | failed`.
- Key assertions: HTTP 202, `job_id`, `submission_source=external_api`, `external_correlation_id`, XML-only `outfmt=5`, sanitized upstream errors, status/list/events/manifest/file download behavior.

## Local-Safe Scenarios

Run these before any live submit:

1. External API contract tests:
   `uv run pytest -q api/tests/test_external_blast_api.py`
2. Dashboard submit options and compatibility gates:
   `uv run pytest -q api/tests/test_blast_submit_route_options.py api/tests/test_blast_compatibility.py`
3. Queue visibility and active status accounting:
   `uv run pytest -q api/tests/test_blast_queue.py api/tests/test_blast_jobs_routes.py`
4. Worker/task route and broker failure behavior:
   `uv run pytest -q api/tests/test_blast_tasks.py api/tests/test_blast_execution_steps_route.py`
5. OpenAPI rate limit and overload response shape:
   `uv run pytest -q api/tests/test_openapi_rate_limit.py`
6. UI New Search/API Reference/cluster context unit coverage:
   `npm --prefix web run test -- usePrerequisites useLatestBlastJob clusterContext aks usePrefetchApiReference`
7. Mocked Playwright coverage:
   `scripts/dev/e2e-ui.sh bypass --headless --fullstack -- npm --prefix web run e2e:all-safe`
8. API smoke without real job creation:
   `scripts/dev/e2e-ui.sh bypass --headless --fullstack -- npm --prefix web run e2e:api-blast`

## Live Submit Scenarios

Use small inputs and existing prepared DBs. Record every job id and request id.

1. Single dashboard submit:
   - Submit one dashboard-style payload through `/api/blast/jobs` without `query_fasta`.
   - Poll `/api/blast/jobs/{job_id}`, `/events`, and `/queue`.
   - Confirm progress leaves `queued` or fails with a user-visible reason.
2. Single OpenAPI submit:
   - Submit one `query_fasta` payload through `/api/v1/elastic-blast/submit`.
   - Poll `/api/v1/elastic-blast/jobs/{job_id}` and list endpoints.
   - Confirm status vocabulary normalization and result file manifest if completed.
3. Canonical inline OpenAPI submit:
   - Submit the same `query_fasta` through `/api/blast/jobs`.
   - Confirm `job_id_kind=openapi`, `dashboard_job_id`, `openapi_job_id`, and dashboard job list visibility.
4. Idempotency replay:
   - Repeat a submit with the same `idempotency_key`.
   - Expected behavior is the same logical job or an intentional upstream idempotency result, not duplicate uncontrolled work.
5. Invalid payload hardening:
   - Non-FASTA query, invalid `program`, non-XML `outfmt`, invalid taxonomy toggle, and oversized/invalid ids.
   - Expected behavior is 4xx with structured sanitized detail, never 500.

## Parallel Submit Scenarios

Parallel probes must be explicitly selected because they can create cost and load.

For `scope: full-azure concurrency=2`, run the full lifecycle scenario with one Playwright worker first. The `concurrency=2` value applies only after that smoke is green, when issuing two simultaneous submit probes against the already prepared environment. Do not parallelize AKS provisioning, DB prepare, sharding, or warmup.

1. Dashboard queue fan-in:
   - Fire `N` dashboard-style submits concurrently, starting with `N=2` and increasing only after clean results.
   - Assert all accepted responses have distinct `job_id` values unless an idempotency key was intentionally reused.
   - Query `/api/blast/jobs/{job_id}/queue` for each job. `queued_count`, `running_count`, and `queue_position` should be internally consistent.
   - Confirm Celery worker capacity: the local runner defaults to main concurrency `4`, but AKS scheduling may limit actual BLAST runtime parallelism.
2. OpenAPI fan-in:
   - Fire `N` OpenAPI submits concurrently with unique `external_correlation_id` values.
   - Assert HTTP 202 under the rate limit. If HTTP 429 appears only above the configured limit, treat it as expected backpressure and verify the response body.
   - Confirm no request returns 500, leaks a token, or loses correlation ids.
3. Mixed fan-in:
   - Submit dashboard and OpenAPI jobs at the same time.
   - Confirm `/api/blast/jobs` merges local Table rows and external OpenAPI rows without duplicates or missing active jobs.
4. Worker-loss/reconcile probe:
   - Only in a controlled local or disposable environment, interrupt a worker after queueing jobs.
   - Confirm `reconcile_stale_jobs` marks lost work or recovers from Kubernetes/OpenAPI state instead of leaving stale `running` rows.

## Time Budget Rules

For `max-hours=4`, prefer cached/reused Azure resources. Before starting the full lifecycle scenario, check whether the target AKS cluster, Storage account, ACR, and `core_nt` database state are already present or cheap to reuse. If the run would need a fresh full DB download, long sharding rebuild, or first-time warmup that cannot fit in the budget, stop with evidence and do not leave an unattended long-running command.

When the lifecycle smoke passes with time remaining, use the remaining budget in this order:

1. One dashboard submit.
2. One OpenAPI submit.
3. Two-way parallel submit fan-in when `concurrency=2`.
4. App Insights query pass covering the run window.

## UI Scenarios

1. Route render smoke: `/`, `/blast/submit`, `/blast/jobs`, `/docs`.
2. New Search payload matrix: representative option changes produce valid payloads and do not regress pre-flight/submit parity.
3. API Reference try-it path: token state, cluster context, OpenAPI submit examples, response panels, and error display.
4. Jobs page: active job chip, status transitions, events, queue state, external OpenAPI jobs, deletion/cancel mocked mutations.
5. Failure display: 401, 403, 409/503 cluster-not-ready, 422 validation, 429 rate limit, upstream 5xx, and degraded monitoring states.

## Evidence To Capture

- Commands run and exact scope variables.
- HTTP status, response body shape, request id, job id, task id, and correlation id.
- Queue snapshot for active jobs.
- Worker/api/web log tails with tokens redacted.
- App Insights query time range and summarized error rows.
- Screenshots or Playwright traces for UI failures.