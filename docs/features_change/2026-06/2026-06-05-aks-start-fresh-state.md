# AKS start: kill the "UI stays Starting until refresh" lag

## Motivation

After clicking **Start** on a stopped AKS cluster, the cluster card could keep
showing `Starting…` even after the cluster had actually settled
(`provisioning_state="Succeeded"`), until the user manually refreshed the page.
The state would only update on the next manual reload, making Start feel
unresponsive.

## Root cause

Two combining issues, both downstream of the monitor snapshot cache:

1. **The monitor cache is per-process, so the lifecycle task cannot invalidate
   it.** `/api/monitor/aks` caches the cluster list (30 s TTL +
   stale-while-revalidate) inside the `api` sidecar. The `start_aks` Celery task
   runs in the **`worker`** sidecar — a separate process — so it has no way to
   drop the `api` process's cached `provisioning_state="Starting"` snapshot when
   the start LRO completes. The SPA therefore kept seeing `Starting` until the
   cache TTL/stale window expired and a poll happened to trigger a background
   ARM re-query.

2. **The 10 s "fast-poll" during a transition never fired.** Both transition
   poll effects in `useClusterActions.ts` listed the whole `query` object in
   their dependency arrays. `query` is a fresh reference every render, so the
   `setInterval` was torn down and recreated on every parent render and the
   5 s / 10 s ticks rarely elapsed. The SPA fell back to the 30 s base poll,
   compounding issue (1).

A manual refresh "fixed" it only because it forced an immediate fetch that
eventually rode the stale-while-revalidate path to a fresh ARM read.

## User-facing change

- While a cluster start/stop transition is in flight, every poll now asks the
  backend for a **cache-bypassed** (`fresh=true`) ARM read, so the
  `provisioning_state` label settles to `Succeeded` the moment ARM flips —
  no manual refresh needed. Once the transition chip clears, normal 30 s
  caching resumes.
- The 5 s task-status poll and the 10 s fast-poll now actually run for the full
  duration of a transition.

## API / code change summary

- `api/services/monitor_cache.py`: `cached_snapshot(...)` gains a keyword-only
  `force: bool = False`. When `True` it bypasses both the fresh-hit and
  stale-hit early returns and refreshes synchronously (still storing the result
  for subsequent normal reads), and never queues a background refresh.
- `api/routes/monitor/aks.py`: `GET /api/monitor/aks` gains a `fresh: bool`
  query param mapped to `cached_snapshot(force=fresh)` on both the RG-scoped and
  subscription-wide paths. Documented as the cross-process-safe way to read
  authoritative ARM state during a transition.
- `web/src/api/monitoring.ts`: `monitoringApi.aks(sub, rg?, { fresh })` adds the
  optional `fresh` flag (sets `?fresh=true`).
- `web/src/components/cards/ClusterCard/useClusterActions.ts`: exported a pure
  `hasActiveClusterTransitions(sub, rg)` reader (reuses the persisted-transition
  store); changed both transition poll effects to depend on the stable
  `query.refetch` instead of the per-render `query` object.
- `web/src/components/cards/ClusterCard/ClusterCard.tsx`: the AKS cluster-list
  query's `queryFn` now reads `hasActiveClusterTransitions(...)` at call time and
  passes `{ fresh }`, so polls during a transition bypass the cache.

No IaC change. No new dependency.

## Validation

- `uv run pytest -q api/tests/test_monitor_cache.py api/tests/test_monitor_aks_fresh.py`
  → 19 passed (2 new `force`-bypass unit tests + 2 new route `fresh` tests).
- `uv run pytest -q api/tests` → 2818 passed, 3 skipped (the lone
  `test_terminal_exec::test_run_truncates_stdout_above_cap` failure is a
  pre-existing parallel-timing flake — passes in isolation — and is unrelated to
  this change).
- `uv run ruff check api/services/monitor_cache.py api/routes/monitor/aks.py …`
  → All checks passed.
- `cd web && npx tsc --noEmit` → clean; `npx eslint` on changed files → clean;
  `npx vitest run transitionTargetReached.test.ts aksStatus.test.ts` → 11 passed;
  `npm run build` → green.
