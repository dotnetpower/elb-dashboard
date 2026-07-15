---
title: Unlock warmup actions after worker result loss
description: Stop stale JobState rows from disabling Warm and Rewarm after a Container App revision replaces ephemeral Redis task results.
tags:
  - blast
  - ui
  - operate
---

# Unlock warmup actions after worker result loss

## Motivation

The `core_nt` cache correctly reported `Failed · 3/10`, but Rewarm and Release
remained disabled. Two durable warmup JobState rows still said `running` after
their worker revision and ephemeral [Redis](https://redis.io/docs/latest/)
results disappeared. The active-warmup endpoint returned those rows, and the
SPA adopted the newest stale task ID and kept polling it as `Pending` forever.

## User-facing change

- Warm and Rewarm no longer remain locked by a warmup row older than the
  canonical two-hour worker-lost threshold.
- Polling an old task ID recovers terminal status from durable JobState when
  Redis no longer knows the task, so the SPA clears its local task handle.
- Recent queued/running warmups continue to fail closed and keep actions
  disabled; the stale threshold remains above the warmup task hard limit.

## API and infrastructure diff summary

- `GET /api/warmup/active` filters proven aged-out warmup rows using the same
  pure classifier as the scheduled stale-dbops reconciler.
- `GET /api/warmup/{instance_id}/status` consults JobState only when Celery
  reports `PENDING`, preserving normal Redis-backed task behavior.
- No response fields, infrastructure, RBAC, Storage, or network settings
  changed.

## Validation evidence

- Focused route and stale-dbops tests: `40 passed`; coverage includes recent
  rows, aged-out active rows, and Redis-`PENDING` fallback to `worker_lost`.
- Full backend suite: `4809 passed, 4 skipped`; Ruff lint passed.
- Documentation frontmatter guard and `mkdocs build --strict` passed.
- Live remediation released only the old Kubernetes warmup resources, then
  started task `672fc400-7127-4b3c-83d5-4aca4ac2116b`. All ten node-pinned Jobs
  completed in approximately five seconds by reusing the node-local cache.
- The durable stale reconciler scanned two legacy rows and terminalised both
  with zero errors. Live endpoints then returned `active=false` and
  `core_nt Ready · 10/10`.
- Design self-review found no unresolved Critical or High issues across state
  transitions, bounded liveness, idempotency, security, failure handling, or
  backward compatibility.