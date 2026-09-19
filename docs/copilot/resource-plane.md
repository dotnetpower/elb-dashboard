---
title: Resource Plane (Agent Detail)
description: Current FastAPI and Celery ownership map for Azure resource operations, BLAST execution, database lifecycle, external messaging, upgrades, and scheduled reconciliation.
tags:
  - agent
---

# ElasticBLAST Resource Plane (detail)

> Re-verified 2026-09-16 against `api/main.py`, `api/celery_app.py`, and
> `api/run_celery_workers.py`.

The web app is the control surface for the infrastructure used by
ElasticBLAST. Bounded reads and small wizard operations may complete inside a
FastAPI request. Long-running or retryable work is implemented as
[Celery](https://docs.celeryq.dev/en/stable/) tasks under `api/tasks/`, queued
through the in-revision Redis sidecar, and executed by the `worker` sidecar.
The API returns a task or operation id; the SPA polls the corresponding status
route and also receives job-change invalidations over SSE.

## Request Versus Task Boundary

- `api/routes/arm.py`, `api/routes/resources.py`, and the monitor packages own
  bounded discovery, response shaping, and small idempotent setup calls.
- Routes enqueue any operation that can exceed an HTTP request budget and
  return `202 Accepted` with durable identifiers.
- Azure SDK calls stay in `api/services/`; Kubernetes calls use the direct
  `api.services.k8s` client; CLI-only work uses the authenticated terminal
  sidecar exec channel.

## Task Families And Queues

| Task family | Main responsibilities | Queue / worker owner |
| --- | --- | --- |
| `api.tasks.azure.*` | AKS provision/start/scale/stop/delete, runtime RBAC, idle auto-stop, and observability setup | `azure` on `worker-main`; periodic reconciliation may target `reconcile` |
| `api.tasks.acr.*` | Build the pinned runtime and OpenAPI images through ACR Tasks | `acr` on `worker-main` |
| `api.tasks.storage.*` | Prepare/update databases, warm node caches, build/retain order oracles, purge aged results, and repair database state | interactive work on `storage`; periodic repair on `reconcile` |
| `api.tasks.blast.*` | Submit/cancel/status, split merge, retry, runtime-metric backfill, and job reconciliation | `blast` on `worker-main`; periodic repair on `reconcile` |
| `api.tasks.blast.artifacts.*` | Capture and reconcile terminal Kubernetes artifacts after a run | `blast-artifacts` on `worker-artifacts` |
| `api.tasks.openapi.*` | Deploy/rebuild OpenAPI, configure public HTTPS, and reconcile its runtime endpoint | interactive work on `azure`; periodic repair on `reconcile` |
| `api.tasks.servicebus.*` | Drain requests, publish durable transitions, reconcile DLQ responses, and emit health | `servicebus` on `worker-servicebus` |
| `api.tasks.upgrade.*` | Check releases, build and deploy revisions, rollback, and compact upgrade history | interactive work on `default`; periodic repair on `reconcile` |
| `api.tasks.webhooks.*` | Deliver configured terminal-job notifications | `reconcile` |

The worker sidecar launches four isolated Celery parents: `worker-main`
(`default,acr,azure,blast,storage`, concurrency 2), `worker-reconcile`
(`reconcile`, concurrency 1), `worker-servicebus` (`servicebus`, concurrency 1),
and `worker-artifacts` (`blast-artifacts`, concurrency 1). This prevents slow
maintenance or external-message work from starving interactive operations.

## Durable State Contract

Tasks must be **idempotent**, side-effect-tagged in their docstrings, and write
progress checkpoints through `api/services/state_repo.py`. Redis is ephemeral:
the durable authority for status, history, schedules, configuration, and
recovery is Azure Table/Blob Storage. Beat reconcilers reconstruct work after a
revision restart rather than trusting Celery result state.

Image tags must stay in sync with `src/elastic_blast/constants.py` in the
sibling repository. Every image-building task reads the single `IMAGE_TAGS`
mapping in `api/services/image_tags.py`; documentation should link that source
instead of copying the current OpenAPI tag.
