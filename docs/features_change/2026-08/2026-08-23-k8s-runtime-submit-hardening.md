---
title: Kubernetes runtime and BLAST submit hardening
description: Recover the overloaded AKS execution plane, bound transient Kubernetes retries, garbage-collect terminal runtime objects, synchronize OpenAPI webhook tokens, and prevent Service Bus transition polls from exceeding worker deadlines.
tags: [blast, operate, security]
---

# Kubernetes runtime and BLAST submit hardening

## Motivation

External job `f2a641ea1403` failed before any BLAST shard was created. Its
`elastic-blast submit` process received `503 ServiceUnavailable` while deleting
one completed setup Job and treated the transient response as a terminal
submission failure. Two other recent failures showed the same signature on
independent `kubectl get` and `kubectl label` calls.

The [Azure Kubernetes Service](https://learn.microsoft.com/azure/aks/what-is-aks)
control plane contained 209,755 Job objects, 20,040 ConfigMaps, and 1,022 Pods.
The deployed `elb-openapi:4.28` batch templates did not carry
`ttlSecondsAfterFinished`; cluster-wide Job list calls consequently produced
429/503 responses and caused the hourly runtime-metrics backfill to hit its
Celery deadline.

Two independent control-plane faults were also active:

- the OpenAPI API token and terminal-transition webhook token differed, causing
  1,346 authenticated webhook requests to return 401; and
- 70 [Azure Service Bus](https://learn.microsoft.com/azure/service-bus-messaging/service-bus-messaging-overview)
  request messages remained admission-blocked by a failed AKS start generation.

## User-facing change

- BLAST status reads now use the job's `elb-job-id` label as a server-side
  selector instead of downloading every historical `app=blast` Job.
- Replay-safe `kubectl get`, `logs`, `apply`, `label --overwrite`, and
  `delete --ignore-not-found` operations retry up to six times for one bounded class of transient
  API failures. Authentication, authorization, usage, and unsafe mutation
  failures remain immediate.
- OpenAPI token generation, rotation, and drift repair update the API and
  webhook env entries in one resource-version-guarded JSON Patch. The
  control-plane process synchronizes both aliases as well.
- A five-minute, Redis-single-flight runtime collector removes bounded batches
  of old terminal Jobs and terminal OpenAPI ConfigMaps. It preserves active,
  recent, malformed, and timestamp-unclassifiable objects. Azure Table and Blob
  Storage remain the durable job/result sources after Kubernetes GC.
- Service Bus transition polling uses a per-call timeout and a task-wide budget
  below the Celery soft/hard deadlines. Process-control timeouts always
  propagate instead of being converted into a successful tick.
- Request queue and completion subscription expiration now dead-letter expired
  messages instead of dropping them silently.
- The browser-terminal banner now verifies the managed-identity login already
  established at startup instead of asking every session to run an unnecessary
  device-code login. Users can still override the identity interactively when
  needed.

## Code and IaC diff

- `api/services/k8s/blast_status.py` scopes status reads by `elb-job-id` and
  rejects invalid label values before opening a Kubernetes session.
- `api/services/k8s/client.py` provides one Retry-After-aware retry for
  idempotent GET responses with status 429/500/502/503/504.
- `api/services/k8s/runtime_gc.py` and
  `api/tasks/blast/runtime_gc_task.py` implement bounded terminal-object GC,
  single-flight locking, configurable retention, structured telemetry, and
  fail-closed timestamp parsing.
- `api/services/openapi/token.py` synchronizes both token env entries and uses a
  resourceVersion test to prevent positional JSON Patch races.
- `api/tasks/servicebus/tasks.py` bounds sibling status polls and preserves
  `SoftTimeLimitExceeded` process-control flow.
- `terminal/patch_elastic_blast.py` injects replay-safe transient retries and
  1,800-second Job TTLs. `scripts/dev/patch-openapi-build-context.py` verifies
  source, system-Python, and venv template copies at image build time.
- `infra/control-plane-env.json` and
  `infra/modules/containerAppControl.bicep` enable the bounded GC worker and
  persist its retention/delete/deadline policy across full and quick deploys.
- The pinned OpenAPI image is `elb-openapi:4.31`, built from sibling commit
  `352a1f4` plus the dashboard patch layer. ACR run `de53` pushed digest
  `sha256:ed8b67d74254fc05985060c38e4a1e348327b4947653dbd0b85d9798258da4e0`.

No route gained broader authorization, no RBAC role was added or removed, no
browser SAS path was introduced, and Storage public network access was not
changed.

## Operational recovery

The existing cluster was recovered in bounded, state-aware batches:

- confirmed zero running `app=blast` and `app=setup` Pods;
- removed only Jobs with succeeded state or an explicit terminal Failed
  condition;
- restored 85 current terminal OpenAPI job ConfigMaps before restarting the
  OpenAPI Pod;
- removed 352 non-terminal ConfigMaps that were older than 24 hours and absent
  from the live sibling job set; and
- rolled the OpenAPI Deployment to 4.31 while preserving all 85 visible jobs.

Post-cleanup API-server storage dropped to 10 Jobs, 104 ConfigMaps, and 109
Pods. API-server memory fell from repeated
91-99% readings to 23%; CPU was 28%. The 4.31 Pod loaded 85 jobs from ConfigMaps,
reported Ready with zero restarts, and exposed the same 82 completed / 3 failed
job distribution.

## Validation

Focused validation completed during implementation:

- `uv run pytest -q api/tests` — 5,039 passed, 4 skipped; the six warnings are
  pre-existing duplicate OpenAPI operation IDs.
- `uv run pytest api/tests -m 'slow or subprocess'` — 83 passed.
- `uv run pytest -q api/tests/test_k8s_blast_status.py api/tests/test_k8s_runtime_gc.py api/tests/test_terminal_patch_elastic_blast.py api/tests/test_servicebus_tasks.py api/tests/test_blast_tasks.py api/tests/test_celery_queue_isolation.py` — 300 passed.
- Token/GC/deploy-contract suites — 41 passed.
- OpenAPI build-context and ElasticBLAST patch suites — 32 passed after the
  final fixture/lint pass.
- K8s transient GET retry/status/GC/warmup suites — 24 passed.
- `uv run ruff check` on all touched Python files — clean.
- `az bicep build --file infra/main.bicep --stdout` — succeeded.
- `uv run python scripts/docs/check_frontmatter.py` — 61 navigated pages valid.
- Live webhook replay for the existing failed job returned HTTP 202 without
  exposing either token.
- Live 4.31 image verification found one retry helper marker and one
  `ttlSecondsAfterFinished: 1800` field in each installed runtime template;
  API/webhook token values matched.
- Service Bus administration re-read confirmed
  `dead_lettering_on_message_expiration=true` on both the request queue and the
  `default` completion subscription.

The Container App and terminal images are rebuilt/redeployed for this change
because the defect lives in the terminal/OpenAPI toolchain and the production
AKS object lifecycle; host-mode Tier 2a cannot validate image-installed
ElasticBLAST templates.

## Rollback

- Set `K8S_RUNTIME_GC_ENABLED=false` to stop all scheduled runtime deletion
  immediately; the current task checks the gate before acquiring its lock.
- Roll the OpenAPI Deployment back to the previous digest if the 4.31 process
  fails readiness. Do not return to 4.28 for steady operation because it lacks
  the verified TTL/retry policy.
- Token patch failure is atomic: the resourceVersion test aborts the complete
  JSON Patch, leaving both existing env entries unchanged.
- Kubernetes GC does not delete Azure Table history or Blob results, so runtime
  object deletion does not require data restoration.
