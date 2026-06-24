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

## Job Detail Metadata & Cache Scenarios

Background: a queue (Service Bus) or external-API job is submitted to the sibling
`/v1/jobs`, which does NOT echo back the BLAST options (outfmt, evalue, …), the
AKS region, or the query identity. The dashboard therefore captures them itself
at drain time (durable stamp on the JobState row), merges live sibling execution
stats on the detail view, and caches both in OPS Redis. These scenarios validate
the completeness, accuracy, and resilience of that enrichment. They exercise the
detail route `GET /api/blast/jobs/{job_id}` and the Run details UI tab
(`BlastJobDetailsGrid`), not the submit/queue paths above.

Unit coverage already exists for the pure pieces (`test_external_query_meta.py`,
`test_external_config.py`, `test_blast_jobs_routes.py`, `configFormat.test.ts`).
The scenarios below close the integration / live / UI gaps.

### Local-safe (mocked / integration)

1. Config-snapshot projection round-trip:
   - Seed a JobState row with `payload.external.config_snapshot` (outfmt, evalue,
     word_size, taxid + is_inclusive, extra).
   - `GET /api/blast/jobs/{job_id}` and assert the response `config_snapshot`
     mirrors every captured key; assert a row WITHOUT a snapshot returns
     `config_snapshot: null` (UI shows "—"), never a fabricated default.
2. Query-identity accuracy (drive `query_meta_from_fasta` through the drain path):
   - Nucleotide 16S fragment → `query_length` equals the residue count and
     `molecule="nucleotide"`.
   - Protein query → `molecule="protein"`.
   - Length counts only alphabetic residues: a FASTA with gaps `-`, stops `*`,
     digits, and interior spaces must not inflate `query_length`.
   - A <4-residue stub → `query_length` present but `molecule` absent (min-scan
     guard), never a confident wrong call.
3. Sibling-stats cache markers:
   - Cold detail load on a completed external job missing `db_version` → one
     live `get_job`, positive stats cached with the 7-day TTL.
   - Warm load → served from cache, NO second `get_job`.
   - Sibling reachable but reports no `db_version` (or `get_job` raises) → a
     short-lived negative marker (5-min TTL) is written so the next loads within
     the TTL do NOT re-fetch; assert the negative TTL < positive TTL.
4. BLAST command preview:
   - Build the command from a captured snapshot and assert the flags
     (`-db`, `-outfmt`, `-evalue`, `-word_size`, `-max_target_seqs`,
     `-negative_taxids/-taxids`, trailing `extra`).
   - De-dup guard: when `extra` already carries a `-outfmt`, the preview must
     contain exactly one `-outfmt`.

### Live-submit

5. Region immediacy on drain (single-cluster subscription):
   - Enqueue one small job; after the ~30 s drain, `GET /api/blast/jobs/{job_id}`
     and assert `infrastructure.region` is populated immediately (no wait for the
     periodic scope-backfill poll).
6. Sibling-stats merge on a completed external job (cache effect):
   - Open the detail of a completed queue/API job whose stored row lacks
     `db_version`. First load fills `db_version` / `blast_version` / `run_seconds`
     from the sibling; record the wall time. Second load returns the same values
     materially faster (cache hit). Capture both `time_total` values as evidence.
7. Query identity on a real submit:
   - Submit the 16S nucleotide fragment; assert the detail shows the expected
     `query_length` + `molecule="nucleotide"` without a Storage blob read.
8. Multi-cluster subscription backfill:
   - In a subscription with >1 ElasticBLAST cluster, a freshly-drained job's
     `region` is blank at drain (ambiguous) and is filled later by the
     scope-backfill poll — assert the eventual non-blank value, and that the
     detail never errors in the interim.

### Resilience / degradation

9. Stopped-cluster sibling fetch:
   - Detail of a completed job whose cluster is Stopped → the first `get_job`
     burns its 10 s timeout once, the negative marker is written, and subsequent
     opens within the TTL render fast. The detail always renders (region/stats
     show "—"), never 500.
10. OPS Redis unavailable:
    - With the cache backend down, every detail load still works (cache no-ops,
      live fetch each time) and never raises; assert HTTP 200 with the merged
      stats still present.
11. Legacy row without captured payload:
    - A pre-feature external job (no `config_snapshot` / `query_meta` / stamped
      region) renders the detail gracefully with "—" / "not recorded for this
      job", no 500, and the optional summary fields are `null`.

### UI (Run details tab)

> Automated in `scripts/e2e/scenarios/blast-run-details.ui.spec.ts` (ui-mock
> lane, part of `e2e:all-safe`). The detail API is fully stubbed; the spec opens
> the job detail URL directly (bypassing the responsive "More" nav grouping) and
> scopes every assertion to the `blast-run-details-grid` test id so the
> Database-metadata card cannot trip strict mode. Stub jobs use a non-terminal
> phase on purpose — BlastResults auto-switches a COMPLETED job off the Run
> details tab to Descriptions, which would unmount the grid mid-assertion.

12. Metadata rows render: open a queue/API job's Run details tab and assert the
    Output format, E-value, Max targets, Word size, Dust, Taxonomy filter,
    Machine, Nodes, BLAST/DB version, Run time, Query length, and Molecule rows
    appear, with the "not recorded for this job" hint on a legacy job.
13. Command + raw panel: assert the full-span **BLAST command** code block is
    copy-friendly and the collapsible **Raw parameters** `<details>` panel shows
    the `config_snapshot` JSON. (Note: the command preview renders the bare
    `blastn -db <db>` from program + db even when no options were captured; only
    the Raw parameters panel is absent for a legacy job.)

## Evidence To Capture

- Commands run and exact scope variables.
- HTTP status, response body shape, request id, job id, task id, and correlation id.
- Queue snapshot for active jobs.
- Worker/api/web log tails with tokens redacted.
- App Insights query time range and summarized error rows.
- Screenshots or Playwright traces for UI failures.