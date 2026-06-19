---
title: Message Flow card — background external-jobs sync for fast first paint
description: The dashboard Message Flow card no longer blocks its first render on a synchronous external OpenAPI /v1/jobs discovery+sync (cluster discovery plus a per-cluster ~10s K8s probe). The sync now runs on a single-in-flight background thread, so the card paints immediately from current Table state and surfaces directly-submitted external jobs on the next poll.
tags:
  - ui
  - blast
---

# Message Flow card — background external-jobs sync for fast first paint

## Motivation

The dashboard **Message Flow** card hides itself until its dedicated
`GET /api/monitor/message-flow` endpoint returns an enabled snapshot. That first
(cold) request was slow: `build_message_flow` ran the external OpenAPI
`/v1/jobs` discovery + sync **synchronously on the request path**, paying an ARM
`managedClusters.list` round trip plus a per-cluster ~10 s-timeout Kubernetes
service-IP probe (worse when a cluster is Stopped/unreachable). The card
therefore appeared on the dashboard several seconds after every other card — the
"Message Flow shows up much later" symptom.

## User-facing change

- The Message Flow card now paints **immediately** from whatever is already in
  the jobstate Table; it no longer waits out cluster discovery + the K8s probe.
- Externally-submitted (`/v1/jobs`) jobs surface on the **next poll** (~8-10 s)
  instead of on the first one. Dashboard-submitted jobs are unaffected — the
  existing submit-time `_invalidate_message_flow_caches` path still reflects them
  immediately.
- No visual or API shape change; the snapshot payload is identical.

## Implementation summary

- `api/services/message_flow.py`
  - `build_message_flow` now calls a new `_spawn_external_sync(tenant_id=…)`
    instead of the synchronous `_sync_external_jobs_best_effort` on the request
    path.
  - `_spawn_external_sync` runs the sync on a daemon thread (`msgflow-extsync`)
    guarded by a module-level **single-in-flight** flag (`_SYNC_LOCK` /
    `_sync_in_flight`) so overlapping polls spawn at most one worker. The flag is
    cleared both in the worker's `finally` **and** on a `Thread.start()` failure
    (resource exhaustion) so it can never wedge permanently.
  - `_sync_external_jobs_best_effort` now drops the message-flow snapshot cache
    (via `_invalidate_snapshot_cache`) **only when the sync actually changed the
    Table** (`created`/`updated`/`tombstoned`), so a steady state does not
    rebuild + re-spawn every poll. The 70 s external `/v1/jobs` list cache is
    intentionally left intact (the sync just populated it).
- No IaC change.

## Validation evidence

- `uv run ruff check api/services/message_flow.py api/tests/test_message_flow.py`
  — clean.
- `uv run pytest -q api/tests/test_message_flow.py
  api/tests/test_message_flow_cache_invalidation.py` — 30 passed (includes new
  guards: background-thread execution, single-in-flight, spawn-failure flag
  reset, change-gated cache invalidation).
- Adjacent suites `api/tests/test_external_blast_api.py`,
  `api/tests/test_route_contracts.py`, `api/tests/test_blast_jobs_routes.py`
  — 131 passed.
- Self-critique (design rubric) surfaced and fixed a Medium~High finding: a
  failed `Thread.start()` would have wedged the in-flight guard True forever;
  the spawn path now resets the flag on failure.
