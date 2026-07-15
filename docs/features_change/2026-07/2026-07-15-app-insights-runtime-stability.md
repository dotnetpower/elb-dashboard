---
title: App Insights runtime stability remediation
description: Production telemetry findings now drive persistent worker memory recycling, time-index reads, Service Bus lock renewal, fork-safe clients, corrected frontend paths, eager blob streaming, and safer client-error telemetry.
tags:
  - operate
  - security
  - ui
  - blast
---

# App Insights runtime stability remediation

## Motivation

A 30-day [Application Insights](https://learn.microsoft.com/azure/azure-monitor/app/app-insights-overview) and [Log Analytics](https://learn.microsoft.com/azure/azure-monitor/logs/log-analytics-overview) review found active runtime defects rather than isolated historical noise:

- Celery prefork children were still being SIGKILLed because the live revision had lost the configured per-child memory recycle cap.
- The live deployment had also lost the validated job-state time-index flag and repeatedly hit the 5,000-row fallback scan cap.
- Service Bus handlers lost message locks before settlement, pooled Table connections dropped history appends, and forked workers reused inherited network client state.
- The SPA requested `/api/api/notifications`, blob streaming committed HTTP 200 before its initial Storage read, and browser-error telemetry retained caller UPNs and URL query data.

## User-facing change

- Notification, cost, webhook, and BLAST-template clients call their real API routes instead
  of duplicated `/api/api/*` paths.
- Blob download failures surface before a successful streaming response is committed.
- Cluster lifecycle conflicts wait through bounded Celery retries instead of becoming immediate failed tasks.
- Service Bus messages renew their locks while bounded handlers run, reducing redelivery and dead-letter risk.
- The time-ordered jobs index and worker memory recycle cap now survive both full and fast deployments.

## API / IaC diff summary

- `infra/control-plane-env.json` and `infra/modules/containerAppControl.bicep` persist
  `JOBSTATE_TIME_INDEX_ENABLED=true`, `CELERY_WORKER_MAX_MEMORY_PER_CHILD_KB=250000`,
  `CELERY_WORKER_PREFETCH_MULTIPLIER=1`, `PYTHONFAULTHANDLER=1`, and the Service Bus
  atomic-claim/single-flight gates across the api/worker/beat deployment paths.
- The worker allocation increases from 1 vCPU / 2 GiB to 1.5 vCPU / 3 GiB. The live 2 GiB
  safety rollout still showed child OOM SIGKILLs because post-task recycling cannot stop an
  in-flight memory spike; 3 GiB keeps the eight-process topology within the Container Apps
  4 vCPU / 8 GiB per-replica ceiling.
- `api/services/service_bus.py` registers received messages with `AutoLockRenewer` for a bounded five-minute renewal window.
- `api/celery_signals.py` drops credential, Azure SDK, Kubernetes, State Table, and Redis client pools inherited across a Celery prefork boundary.
- `api/services/state/repository.py` retries a history append once with a fresh Table client and the same row key, treating an uncertain first-write `ResourceExistsError` as idempotent success.
- Subscription/cluster-scoped job listings traverse the immutable global time-index partition
  newest-first and filter current mutable scope fields in bounded batches. This removes the live
  BLAST Jobs page's repeated 5,000-row legacy scan while preserving an empty-index fallback for
  pre-backfill deployments.
- `api/tasks/azure/lifecycle.py` uses bounded delayed retries for ARM lifecycle conflicts.
- `api/services/storage/blob_io.py` performs the initial `download_blob()` call before returning the streaming iterator.
- `web/src/api/notifications.ts`, `web/src/api/cost.ts`, `web/src/api/webhooks.ts`, and
  `web/src/api/blastTemplates.ts` use paths relative to the shared `/api` prefix.
- The authenticated OpenAPI webhook manually validates its body and returns the documented
  202 `invalid_body` envelope for malformed/truncated JSON instead of triggering a retry storm.
- A failed post-lifecycle warmup enqueue rolls back its token-scoped admission correlation,
  releases the in-flight lease, and terminalises the seeded job row so the next reconcile can
  retry safely.
- `STRICT_CLIENT_LOG_REDACTION` ships default-OFF per hardening policy. When enabled, `api/routes/client_log.py` hashes caller identity and strips URL query/fragment data. Planned default flip: 2026-08-15 after one dogfood release and a green Persona Matrix run with the gate forced ON.
- High-volume Service Bus AMQP and successful exporter loggers are pinned to the Azure warning level.

## Validation evidence

- Expanded backend integration across admission, Service Bus, warmup, lifecycle, webhook,
  Storage, telemetry, and deployment-env contracts — 516 passed.
- Frontend path contract: `npm test -- --run src/api/pathContracts.test.ts` — 4 passed.
- Full backend: `uv run pytest -q api/tests` — 4,799 passed, 4 skipped.
- Full frontend: `npm test -- --run` — 968 passed.
- Frontend production build: `npm run build` — passed.
- Backend lint: `uv run ruff check api` — passed.
- Bicep compile: `az bicep build` for `infra/main.bicep` and `infra/modules/containerAppControl.bicep` — passed; generated JSON synchronized.
- Container App module what-if: `Succeeded`, 30 `Ignore`, one expected `Modify`, and no
  `Create` / `Delete` / `Deploy` / `Unsupported` changes.
- Live P0 safety rollout: revision `ca-elb-dashboard--0000224` was created with the time-index,
  worker recycle, faulthandler, atomic-claim, and drain-singleflight values. Redeployment was
  necessary because revision 221 was actively SIGSEGV/SIGKILL-looping and the fault depended
  on the deployed sidecar cgroup/configuration, so it was not reproducible in Tier 1 or
  host-mode Tier 2a. The 2 GiB worker continued to OOM-kill children during the soak, which is
  the evidence for the 3 GiB resource correction above; that corrected image/template still
  requires final live readiness and post-deploy telemetry validation.
- Corrected rollout: API/worker/beat image digest `sha256:867e8f…` and frontend digest
  `sha256:e5c8f7…` converged in healthy revision `ca-elb-dashboard--0000228`; all six sidecars
  reported Ready with restart count zero. Initial startup probes failed while the app booted,
  followed by `RunningAtMaxScale` and three consecutive `/api/health` 200 responses. The first
  post-deploy telemetry window contained zero AppExceptions, failed AppRequests, ERROR traces,
  API native crashes, worker SIGKILL/WorkerLost, and Service Bus settlement failures. The
  corrected frontend emitted `/api/notifications` 200 instead of `/api/api/notifications` 404.
- Final scoped-index image digest `sha256:2a79e7…` converged in revision
  `ca-elb-dashboard--0000231`. All six sidecars were Ready with restart count zero and the
  ingress health response identified revision 231. A real authenticated subscription-scoped
  jobs request returned 20 jobs without degradation, while revision-231 console telemetry
  recorded zero API native crashes, worker SIGKILL/WorkerLost, Service Bus settlement misses,
  and 5,000-row scan-cap warnings. App Insights recorded zero exceptions and ERROR traces; the
  sole failed-request row belonged to revision 228's stale `/api/api/notifications` call during
  cutover, not revision 231. Four startup probe failures occurred before readiness, with zero
  container terminations.
