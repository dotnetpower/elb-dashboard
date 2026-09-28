---
title: Runtime reliability and telemetry hardening
description: Bound time-index repair work, preserve background task progress, correct OpenAPI identity use, and reduce false runtime failures found in a seven-day telemetry review.
tags:
  - operate
  - blast
  - infra
---

# Runtime reliability and telemetry hardening

## Motivation

A seven-day [Application Insights](https://learn.microsoft.com/azure/azure-monitor/app/app-insights-overview) and [Azure Container Apps](https://learn.microsoft.com/azure/container-apps/overview) log review found no failed browser/API requests, but it exposed several background-control-plane defects:

- hourly time-index reconciliation ran 166 times, scanned 4,015,711 JobState rows, repaired no rows, generated 16,015,589 dependency spans (19.06 GiB), and occupied the single reconcile worker long enough for 2,836 maintenance ticks to expire;
- the stale-job reconciler sent canonical `job-<32hex>` ElasticBLAST runtime identities to a sibling endpoint that accepts only a 6-12 hex OpenAPI queue id, producing 688 invalid-id warnings;
- oversized progress transcripts caused two [Azure Table Storage](https://learn.microsoft.com/azure/storage/tables/table-storage-overview) `PropertyValueTooLarge` rejections;
- an Oracle reconciler could swallow a [Celery](https://docs.celeryq.dev/en/stable/) soft deadline and continue until hard kill, while DLQ reconciliation could serially process up to 100 messages without its own pass deadline;
- historical terminal jobs retried pod-log capture after the Kubernetes pod-retention window, and readiness probes intermittently exceeded their three-second timeout;
- long-lived worker/beat periodic metrics exporters duplicated the existing cgroup metrics stream and emitted repeated non-retryable exporter transport failures.

## User-facing change

- Recent searches and background maintenance remain responsive while time-index repair advances in bounded, durable batches.
- Dashboard jobs no longer generate invalid sibling status requests; external and Service Bus jobs continue to refresh through their short OpenAPI ids.
- Large command-output tails can no longer block the durable status/phase transition of a job. Full output remains in job-log artifacts.
- Historical jobs report pod logs as outside the retention window instead of repeatedly failing capture.
- Readiness tolerates a brief scheduler stall without changing liveness, authentication, RBAC, or private-network policy.

## API and IaC diff summary

- `JobStateRepository.reconcile_time_index_batch` scans at most 1,000 source rows per periodic pass. A durable singleton cursor advances only after success; the existing full-scan method remains available to the one-shot backfill.
- Read-only index existence checks suppress redundant dependency spans. Actual index creates, summary logs, and task failures remain observable.
- The periodic repair cadence is 15 minutes with a 180/210-second soft/hard limit, a 240-second lock TTL, and a 300-second queue expiry.
- Stale-job reconciliation separates the Kubernetes runtime id from the sibling OpenAPI id and excludes pre-submit Service Bus placeholders.
- JobState payload serialization enforces a 60 KiB UTF-16 budget by compacting only mirrored progress output. If unrelated payload data still cannot fit, the payload patch is skipped while scalar state columns are persisted.
- Oracle broad catches re-raise `SoftTimeLimitExceeded`. DLQ reconciliation processes at most 16 messages under a 70-second monotonic budget and abandons untouched messages for the next tick.
- Worker and beat keep traces and `api.*` logs but disable redundant standard OpenTelemetry metrics by default; the API role remains unchanged and an explicit background-metrics opt-in remains available.
- Readiness single-flight slots are released only by their leader, preventing a bounded follower fallback from waking other callers into duplicate sibling probes.
- Indexed listings read the `limit + 1` probe across Azure Table's 1,000-row page boundary, so a 1,001st row cannot disappear with a false end cursor.
- The Service Bus worker prefetches one task; transition ticks expire before their next schedule; DLQ cleanup has explicit 120/150-second limits and a five-minute queue expiry.
- Both the periodic Oracle dispatcher and the long-running Oracle Kubernetes task propagate every Celery soft deadline instead of converting it into a recoverable dependency error.
- External-job cancellation fills routing scope from durable JobState columns before payload fallback, avoiding an incorrect default-cluster endpoint.
- API and terminal readiness timeout values increase from three to five seconds. Public network access remains disabled for ACR and Storage.
- The postprovision Bicep swap loads azd-only environment pins and preserves the Service Bus, date-layout, and NCBI Direct deployment overrides.
- Rejected oversized payload patches leave both the stored payload and its canonical metadata columns unchanged; independent runtime-identity backfill still proceeds when optional evidence cannot fit.
- Equal-timestamp local and external jobs use the same immutable RowKey tie-breaker as the cursor, and the reconcile batch knob also bounds the actual Azure Table page size.
- Runtime-identity conflicts now fail closed before status, evidence, telemetry cache, or webhook transitions can mix generations. Legacy external rows without `submission_source` retain short-id refresh compatibility.
- Ready artifact rows whose JSON or gzip body is corrupt transition to `failed` (`invalid_json` / `invalid_gzip`) so the next request can rebuild them.
- Standalone postprovision loads azd values before required-variable validation. Image-only no-build deployment opens a bounded ACR data-plane lease with restore armed before mutation; the redundant GitHub Actions tag precheck was removed in favor of the script's fail-closed digest resolution.

## Validation evidence

Focused checks completed during implementation:

- `uv run pytest -q api/tests/test_jobstate_time_index.py` - 36 passed.
- Four stale-reconciler identity-contract tests - 4 passed.
- JobState/progress/pod-log focused selection - 60 passed.
- Payload compaction and scalar fallback tests - 2 passed.
- `uv run pytest -q api/tests/test_reconcile_oracle_dispatches.py` - 13 passed.
- Service Bus DLQ durability/deadline tests - 2 passed.
- Pod-log retention and exhaustion tests - 2 passed.
- `uv run pytest -q api/tests/test_telemetry_init.py` - 13 passed.
- Container App readiness probe contract - 1 passed.
- Twelve design-critique rounds covered state contracts, liveness, concurrency, partial failure, security, observability, compatibility, Azure Table limits, Celery topology, and deployment parity. The closure round found no Critical, High, or Medium findings.
- A second, separate 12-round hardening cycle repeated those lenses against the expanded diff. Its closure gate also found no Critical, High, or Medium findings; only bounded or intentional Low residuals remain.
- Final `uv run pytest -q api/tests` - 6,015 passed, 4 fixture-dependent parity tests skipped.
- Related post-format regression sweeps - 774 passed, then 837 passed after the second hardening cycle.
- `uv run ruff check api` - passed; all 34 touched Python files pass `ruff format --check`.
- Production mypy debt baseline unchanged: 500 errors across 97 files.
- OpenAPI contract unchanged at 242 operations; generated TypeScript types are current.
- `bash -n` for postprovision, quick-deploy, ACR access, and env-loader scripts plus `scripts/dev/tests/test_lib_env.sh` - passed.
- `az bicep build --file infra/main.bicep --stdout` - passed.
- Resource-group `what-if` reported one `Deploy` change for the bundled Container App and no delete, RBAC, ACR, Storage, Key Vault, VNet, or private-endpoint changes. Subscription-scope preview was unavailable because the caller lacks `Microsoft.Resources/deployments/whatIf/action` at subscription scope.
- Docs frontmatter and strict MkDocs build - passed.

No live redeploy was performed: code changes are validated locally, and the probe/template change requires the normal reviewed full Bicep/postprovision deployment rather than a quick image-only patch.
