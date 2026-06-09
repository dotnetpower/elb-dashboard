---
title: Make background (auto) warmup transparent
description: Surface in-flight warmups the browser did not start so the user always sees the warmup steps, including auto-warmup triggered by the reconciler or an AKS start.
tags:
  - blast
  - ui
---

# Warmup transparency — show the steps for auto-warmup too

## Motivation

User-initiated warmup already rendered live phases in the cluster's WarmupSection
because `/api/warmup/start` returned a Celery task id the SPA could poll.
**Auto-warmup** — triggered by the beat reconciler or the forced re-warm after
an AKS start — ran with no browser round-trip, so the user had no idea databases
were being warmed in the background and never saw the steps.

## User-facing change

- The WarmupSection now discovers in-flight warmups for the cluster (including
  auto-warmups) and renders their live phase progression (queued → checking
  storage → building shards → planning → applying jobs → loading DB to nodes →
  completed). An **Auto** badge marks platform-initiated runs.
- Even before a warmup's Celery task id is attached, a lightweight banner shows
  the database and current phase, so a background warmup is never invisible.

## API / IaC diff summary

- New route `GET /api/warmup/active?subscription_id&resource_group&cluster_name`
  — scans the `jobstate` table for active (`queued`/`running`) `warmup` rows
  scoped to the cluster and returns each one's `instance_id` (Celery task id),
  `db`, `phase`, `status`, and an `auto` flag. Best-effort (never 500).
- New TS types `WarmupActiveItem` / `WarmupActiveResponse` + `warmupActive`
  client method.

## Code summary

- [api/routes/warmup.py](../../../api/routes/warmup.py) — `warmup_active` route.
- [web/src/api/monitoring.ts](../../../web/src/api/monitoring.ts) — types +
  `warmupActive`.
- [web/src/components/WarmupSection.tsx](../../../web/src/components/WarmupSection.tsx) —
  poll `/warmup/active`, adopt the active warmup, render the Auto badge + banner.

## Validation

- `uv run pytest -q api/tests/test_warmup_route.py` — new `warmup_active` tests
  (in-flight, cluster filtering, state-store fault) pass.
- `cd web && npm run build` clean.
