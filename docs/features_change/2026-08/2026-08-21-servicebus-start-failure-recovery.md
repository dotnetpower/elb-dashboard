---
title: Service Bus failed-start admission recovery
description: Reconcile a terminal AKS start failure only after the live cluster, workload nodes, OpenAPI plane, and node-local database warmup have independently converged.
tags:
  - blast
  - architecture
  - operate
---

# Service Bus failed-start admission recovery

## Motivation

The [Azure Service Bus](https://learn.microsoft.com/azure/service-bus-messaging/service-bus-messaging-overview)
request queue remained closed after an [Azure Kubernetes Service
(AKS)](https://learn.microsoft.com/azure/aks/what-is-aks) start task recorded a terminal
failure. Later successful AKS operations restored the cluster to `Running/Succeeded`, but the
durable failed lifecycle generation continued to return `aks_start_failed` before live readiness
was evaluated. The resident consumer therefore never opened a receiver, so queued requests did
not reach submit or acknowledgement processing.

## User-facing change

- A terminal **start** failure remains fail-closed until the current execution plane independently
  proves all existing safety conditions: AKS/OpenAPI readiness, exact target workload-node count,
  every target node Kubernetes Ready, no active warmup, and non-degraded database readiness.
- A failed start that names warmup databases additionally requires an authoritative node-local
  `warmup` Job source, one consistent database `source_version`, no active/failed warmup Jobs, and
  enough successful Jobs for every target node. Setup-only caches and stale/partial warmups do not
  qualify.
- After that proof succeeds, the drain cancels only the recovered barrier token and emits the
  `servicebus_admission_recovery` feature event before opening the receiver. A concurrent newer
  lifecycle token is unaffected, and the existing pre-submit admission check still closes the
  receive-to-submit race.
- If the cancellation write fails, the already-proven drain may proceed while the next tick repeats
  the complete live proof. Repeated warnings are deduplicated in the existing five-minute window.

Normal ready admission, start/scale/stop/delete lifecycle behavior, Service Bus message schemas,
HTTP response schemas, queue settlement, retry policy, and frontend controls are unchanged. No new
feature flag, dependency, role assignment, Azure resource, public network path, or SAS token is
introduced.

## Current incident recovery before deployment

Do not purge or resend the request queue. Wait for any directly submitted BLAST job to finish, then
invoke the existing authenticated `POST /api/aks/start` endpoint once with only the cluster scope:

```json
{
  "subscription_id": "<customer-subscription-id>",
  "resource_group": "rg-elb-cluster",
  "cluster_name": "elb-cluster-01"
}
```

The existing task treats an already-Running cluster as an idempotent no-op, creates a new lifecycle
generation, loads the saved Auto warmup preference, and forces the token-correlated database rewarm.
Poll the returned task ID until success, then verify `core_nt` is Ready on all workload nodes and the
request queue's active count decreases. Do not run Stop/Start in the Azure portal and do not purge the
queue; both create unnecessary interruption or data-loss risk.

## Ten-round design critique

1. **State machine:** recovery is start-only; scale/stop/delete failure semantics are unchanged.
2. **Liveness:** recovered allow decisions are never cached; node regression closes the next check.
3. **Concurrency:** old-token cancellation cannot open a superseding stop/delete generation.
4. **Partial failure:** strict state reads remain fail-closed; cancellation failure does not corrupt
   settlement or erase another token.
5. **Warmup correctness:** setup-only, partial, active, failed, stale, and marker-less warmups deny.
6. **Routing:** missing or ambiguous cluster context remains `cluster_context_unavailable`.
7. **Observability:** logs carry only a truncated random token and exception class; warning bursts
   are deduplicated.
8. **Compatibility:** new decision fields are optional and consumed with `.get()`; public payloads
   are unchanged.
9. **Performance:** the additional warmup fan-out runs only while reconciling the old failed token;
   successful token cancellation returns subsequent checks to the existing path.
10. **Rollout:** no deployment was performed. Rollback is the prior image; the token-scoped cancel
    row is harmless to older code, which already understands cancelled barriers.

## Validation evidence

- Live read-only evidence before the code change: request queue `active=79`, `DLQ=0`; AKS and both
  node pools `Running/Succeeded`; saved Auto warmup enabled for `core_nt` on 10 nodes; live warmup
  `Ready`, `10/10`, `active=0`, `failed=0`, sources `setup,warmup`, with one source-version marker.
- Direct admission/Service Bus/lifecycle consumer sweep — 209 passed.
- `uv run pytest -q api/tests` — 5,020 passed, 4 skipped.
- `uv run ruff check api` — passed.
- `uv run mypy --strict --follow-imports=skip api/services/aks/execution_admission.py` — passed.
- `uv run python scripts/docs/check_frontmatter.py` — passed.
- `DISABLE_MKDOCS_2_WARNING=true uv run mkdocs build --strict` — passed.
- No deployment, queue receive, Service Bus settlement, AKS lifecycle action, or other live mutation
  was performed during validation.
