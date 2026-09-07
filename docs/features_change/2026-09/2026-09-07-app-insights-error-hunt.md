---
title: App Insights error hunt and runtime noise hardening
description: Classify the last 30 days of control-plane telemetry and fix result, pod-log, local telemetry, completed-job scan, and AKS stop-transition defects.
tags: [operate, blast]
---

# App Insights error hunt and runtime noise hardening

## Motivation

A 30-day [Application Insights](https://learn.microsoft.com/azure/azure-monitor/app/app-insights-overview) review mixed real deployment faults with expected HTTP rejections, local `TestClient` traffic, handled [Azure Storage](https://learn.microsoft.com/azure/storage/common/storage-introduction) misses, and bounded retry warnings. The noise hid several current defects: aggregate result budgets were counted as parse failures, Service Bus correlation values delayed ElasticBLAST runtime identity persistence until after pod-log TTL cleanup, local processes could recover the deployed connection string from durable state, the completed-runtime backfill scanned an arbitrary 5,000-row window, and Auto oracle could query [AKS](https://learn.microsoft.com/azure/aks/what-is-aks) while the cluster was stopping.

## Telemetry inventory

The review covered `2026-08-08` through `2026-09-07` in the 90-day workspace retention window and treated API, worker, and beat roles separately.

- Raw signals: 22,480 unsuccessful requests, 3,966 exception items, 60,192 text-matched traces, and 673,013 unsuccessful dependency spans. The generic trace text query was invalid as an error measure because successful structured messages contain fields such as `failed=0`; only severity 3 or higher is an error trace.
- HTTP: 19,918 historical webhook 401 responses ended on `2026-08-23` after token synchronization; 383 `testserver` 5xx and 1,780 `testserver` 4xx responses ended on `2026-08-29`; the live deployment had three recent OpenAPI 503 responses on `2026-09-02` and `2026-09-05`, with none in the following 48 hours.
- Exceptions: the dominant family was AKS connection failure while the cluster was stopped or transitioning. The latest pre-fix burst was five `ConnectTimeout` rows at `2026-09-07T14:15:44Z`, during the idle stop that completed at `14:15:59Z`. Older families included bounded Celery timeouts, Storage/RBAC failures during prior deployments, monitor circuit-breaker duplicates fixed on `2026-08-29`, and expected ownership-conflict guards. No user job was left active by the current burst.
- Dependencies: most raw failures were Azure SDK wrapper spans (`DependencyType=InProc`, `ResultCode=0`) around handled Table/Blob misses. A correlated sample had a nested Azure Table 404 span treated as successful policy. Excluding only those wrapper spans left 5,343 dependency failures over 30 days. The latest non-AKS item was one Blob 504 on `2026-09-07T11:18:54Z`; the same artifact flow subsequently wrote Blob/Table records with 201/204 responses.
- Latest revision warnings: 28 jobs exhausted pod-log capture because their canonical `job-<32hex>` identity arrived after finalization; one large result aggregate skipped four shard reads under the 64 MiB budget but reported them as failures; six obsolete 10-second Service Bus drain ticks expired under their intentional 15-second bound; Redis acquire/release warnings occurred once each during revision startup; one malformed Service Bus request was correctly dead-lettered.

## Changes

- Aggregate builders now classify `ResultReadBudgetExceeded` as an honest partial result (`truncated=true`) instead of a parse/read failure.
- Local API/worker processes no longer auto-recover the deployed Application Insights connection string from durable state. Explicit local connection-string opt-in still works; durable self-healing remains enabled inside Container Apps.
- The OpenAPI build-context patch accepts `correlation_id` as a runtime identity only when it is canonical. Workflow correlation values such as `wf3:...` now fall through to the submit-output parser, so the terminal webhook can carry the runtime ID before Kubernetes TTL cleanup.
- Completed runtime-metric backfill scans only recently updated completed jobs by default (two hours, bounded to seven days), avoiding the arbitrary 5,000-row cap and stale K8s calls. Other `list_completed` consumers retain the all-history default.
- Oracle readiness bypasses the 90-second AKS health cache and blocks both non-running and non-`Succeeded` provisioning states before Storage/Kubernetes probes. ARM lookup failure still degrades open under the existing contract.
- The App Insights hunt reference now uses `ExceptionType`, severity-based trace filtering, and excludes only the known derived `InProc`/result-code-zero dependency artifact.

## Current state

- Container App revision `ca-elb-dashboard--env-beat-1788781349-11597` is Healthy with one replica and 100% traffic.
- Before the idle stop, all 11 AKS nodes were Ready and the OpenAPI deployment had one Ready pod with zero restarts. The cluster is now intentionally Stopped with the blast pool at zero after the 60-minute idle policy.
- A representative completed job retained all 10 result files and a healthy aggregate. Its canonical runtime ID was eventually backfilled, confirming that the remaining impact was missing durable pod tails rather than missing scientific results.
- The malformed Service Bus request reached the request DLQ with `servicebus_malformed_request`; neighboring valid requests continued through bounded readiness deferral and acceptance.
- From the completed idle stop at `2026-09-07T14:16:00Z` through `14:38:06Z`, App Insights reported zero 5xx requests, exceptions, severity-3 traces, or actionable dependency failures.
- A clean local restart left both `APPLICATIONINSIGHTS_CONNECTION_STRING` and `CONTAINER_APP_NAME` absent from the API process. All subsequent `127.0.0.1 /api/health` telemetry belonged to the deployed Container App replica, confirming that host-mode traffic no longer entered the live workspace.

## Validation

- `uv run pytest -q api/tests/test_job_artifacts.py -k 'streaming_aggregate'` - 2 passed.
- `uv run pytest -q api/tests/test_app_insights_pref.py api/tests/test_telemetry_init.py -k 'telemetry_resolver or init_skipped_without_connection_string'` - 2 passed.
- `uv run pytest -q api/tests/test_patch_openapi_build_context.py` - 31 passed.
- `uv run pytest -q api/tests/test_blast_tasks.py -k 'backfill_completed_runtime_metrics'` - 4 passed.
- `uv run pytest -q api/tests/test_state_repo.py -k 'list_completed'` - 3 passed.
- `uv run pytest -q api/tests/test_oracle_build.py` - 11 passed.
- `uv run pytest -q api/tests` - 5,713 passed, 5 skipped.
- `uv run ruff check api scripts/dev/patch-openapi-build-context.py` - passed.
- `uv run python scripts/docs/check_frontmatter.py` - passed.
- `DISABLE_MKDOCS_2_WARNING=true uv run mkdocs build --strict` - passed.
- Live read-only checks: App Insights KQL across requests/exceptions/traces/dependencies, Container App revision health, AKS ARM state, Kubernetes `/readyz`, node readiness, OpenAPI deployment/pod readiness, Service Bus transition logs, and authenticated production result-page response capture.
