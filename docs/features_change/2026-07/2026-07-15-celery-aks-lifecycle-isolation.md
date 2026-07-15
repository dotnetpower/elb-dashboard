---
title: Isolate AKS lifecycle work from periodic reconciliation
description: Prevent slow Service Bus transition polling from starving interactive AKS start and stop tasks in the shared Celery worker sidecar.
tags:
  - architecture
  - blast
  - operate
---

# Isolate AKS lifecycle work from periodic reconciliation

## Motivation

An AKS lifecycle request returned a Celery task ID but remained `PENDING` and
never reached the Azure Resource Manager start operation. Runtime diagnostics
showed all four `worker-main` prefork slots occupied by overlapping Service Bus
transition publishers while the `azure` Redis queue retained the AKS task.
Routing periodic work to a named `reconcile` queue did not provide isolation
because the same worker process consumed both interactive and periodic queues.

## User-facing change

AKS start, stop, scale, and other interactive control-plane actions no longer
share execution slots with periodic reconciliation. When AKS is stopped and the
OpenAPI execution plane is unavailable, Service Bus transition polling defers
without issuing one long status timeout per active bridge. Obsolete periodic
Service Bus ticks expire instead of replaying an accumulated backlog.

## API and infrastructure diff summary

- `api/run_celery_workers.py` starts three isolated prefork workers inside the
  existing worker sidecar: interactive (`default,acr,azure,blast,storage`),
  periodic (`reconcile`), and artifacts (`blast-artifacts`). The default child
  counts are 3 + 1 + 1, so adding the reconcile parent does not increase the
  previous total Python process count.
- `api/tasks/servicebus/tasks.py` checks the bounded OpenAPI readiness endpoint
  before polling active transition bridges and preserves those rows for a later
  tick when the cluster is unavailable.
- `api/celery_app.py` gives frequent Service Bus periodic messages a 30-second
  expiry so stale ticks are discarded after upstream recovery.
- `api/celery_signals.py` starts resident Service Bus/background consumers only
  in `worker-reconcile`, preventing one daemon copy per Celery parent.
- `infra/modules/containerAppControl.bicep` documents the runtime process layout;
  no Container App resource shape or role assignment changes.

## Validation evidence

- Focused tests: `104 passed` across queue isolation, Service Bus tasks,
  worker command construction, Celery failure visibility, and the resident
  consumer.
- Full backend suite: `4759 passed, 4 skipped`.
- Ruff: `uv run ruff check api` passed.
- Documentation: frontmatter guard passed and `mkdocs build --strict` passed.
- Bicep: `containerAppControl.bicep` compiled successfully. The generated ARM
  template is identical to the tracked template except for Bicep generator
  metadata, confirming the comment-only infrastructure diff. Subscription
  `what-if` was attempted but the signed-in caller lacks
  `Microsoft.Resources/deployments/whatIf/action`; no deployment was applied by
  that command.
- Pre-fix live evidence: task `88638fd7-0d11-4bec-a574-3b7ebb3e0d4a` remained
  `PENDING`, Redis queue `azure` depth was 1, all four `worker-main` slots ran
  `publish_transitions` for approximately 25 minutes, and the AKS Activity Log
  contained no matching start operation.
- Deployed revision `ca-elb-dashboard--0000221`: all six sidecars reported
  Ready with zero restarts; `worker-main`, `worker-reconcile`, and
  `worker-artifacts` each answered Celery ping. A live `diag_noop` sent to the
  `azure` queue (`ff02e445-83b7-4a60-ad8d-b8ffb7a96a8e`) reached `SUCCESS`
  immediately, and the follow-up diagnostic showed `azure=0`, no active or
  reserved work on `worker-main`, while `evaluate_idle_clusters` remained
  isolated on `worker-reconcile`.
- Local worker-process smoke was attempted through the canonical launcher but
  this host has no Docker CLI or Redis binary. Runtime validation therefore
  uses the focused topology tests plus the deployed revision check recorded
  after rollout.
