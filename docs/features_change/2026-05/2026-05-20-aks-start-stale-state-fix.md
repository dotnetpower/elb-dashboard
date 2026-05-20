# AKS start: kill the "Cluster is stopped after Start" stale-state window

## Motivation
After clicking **Start** on a stopped AKS cluster (or starting it externally
via `az aks start` / Portal), the dashboard could keep showing
`Cluster is stopped` for up to ~5 minutes even though ARM had already
flipped `power_state` to `Running`. Two combining issues:

1. **Monitor cache lag** — `/api/monitor/aks` uses a 30 s TTL + 300 s
   stale-while-revalidate cache. A fresh hit returns the previous reading;
   a stale hit returns the previous reading *and* triggers a background
   refresh, so the next poll is the earliest the SPA can ever see the new
   state.
2. **`transitioning` map lost on reload** — the React-only `transitioning`
   Map in `useClusterActions` drives the *"Cluster is starting…"* banner
   and the 10 s fast-poll. F5, navigating away, or starting the cluster
   from CLI/Portal wiped it, so the user just saw the stale Stopped
   reading with no transition indicator.

## User-facing change
- Clicking Start / Stop / Delete now causes the **very next monitor poll
  to bypass the cache** and re-query ARM. ARM may still report Stopped
  while the async start completes (1–3 min), but the moment ARM flips,
  the SPA sees it on the next 10 s fast-poll instead of waiting for the
  cache TTL to expire.
- The starting/stopping banner and the 10 s fast-poll now **survive a
  page reload** (10 min localStorage TTL, scoped per
  `subscriptionId:resourceGroup`). The map auto-evicts when the cluster
  actually reaches the expected `power_state`, or after the TTL deadline
  if the operation got stuck.

## API / IaC diff summary
- `api/services/monitor_cache.py` — new `invalidate_monitor_snapshot_prefix(prefix)`
  helper. **Boundary-safe** (`key == prefix` or `key.startswith(prefix + ":")`)
  so `rg-elb-01` cannot invalidate `rg-elb-01-blue`. Bumps `_GENERATION` when
  it removes anything, which makes any in-flight background refresh a no-op
  (closes the race where a refresh queued before invalidation would
  repopulate the cache with the pre-mutation reading).
- `api/routes/aks.py` — `aks_start`, `aks_stop`, `aks_delete` now invalidate
  the cluster-list key (`monitor:aks:{sub}:{rg}`) **plus** every per-cluster
  category produced by `api/routes/monitor.py`: `nodes`, `pods`, `top-nodes`,
  `warmup-status`, `events`. The category list is documented inline so future
  monitor keys must update both sides together.
- `web/src/components/cards/ClusterCard/useClusterActions.ts` —
  `transitioning` map seeded from / persisted to
  `localStorage["elb-cluster-transitions:{sub}:{rg}"]` with a 10 min
  per-entry deadline.

No Bicep / infra changes. No new dependencies.

## Validation
- `uv run pytest -q api/tests` → **692 passed** (was 680; +12 new tests:
  4 in `test_monitor_cache.py` covering boundary-safe match, in-flight
  refresh cancellation, no-match / empty-prefix no-op semantics, and
  3 parametrised cases in `test_warmup_route.py` asserting that every
  AKS lifecycle route invalidates all 6 monitor:aks:* cache key shapes
  for the targeted scope and preserves sibling RG / different-namespace
  keys).
- `uv run ruff check api` → All checks passed.
- `cd web && npx tsc --noEmit` → clean.
- Manual: hit `GET /api/monitor/aks?...` then `POST /api/aks/start`, confirm
  next `GET /api/monitor/aks` reports `cache.state="refreshed"` and
  re-reads ARM (already verified the API path returns `Running` for the
  current cluster during this session).
