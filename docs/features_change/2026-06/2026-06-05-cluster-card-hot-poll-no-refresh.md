---
title: Cluster card refreshes itself while a cluster is provisioning
description: The AKS cluster card now hot-polls (5 s) and bypasses the backend monitor cache while a cluster is provisioning or transitioning, so the optimistic "Starting…" chip clears without a manual page reload.
tags:
  - ui
  - blast
---

# Cluster card refreshes itself while a cluster is provisioning

## Motivation

After clicking **Start** on a stopped AKS cluster from the dashboard (and not
navigating away), the cluster row stayed on `Starting…` indefinitely and only a
manual page reload showed the settled state. The Start action felt broken even
though the cluster had already started.

Root cause: the row's `Starting…` label is an **optimistic transition chip**
held in `localStorage`, not the backend `provisioning_state`. The chip is only
cleared once a poll observes the settled `power_state=Running` +
`provisioning_state=Succeeded` (`transitionTargetReached`). Chip removal
therefore depends entirely on the cluster-list `useQuery` actually refetching
fresh data. Two gaps starved that refetch:

1. The global `QueryClient` defaults (`refetchIntervalInBackground: false`,
   `refetchOnWindowFocus: false`) stop polling when the tab is backgrounded and
   never refetch on focus return.
2. Even in the foreground, the base poll runs at the user's auto-refresh cadence
   (30–60 s) and the backend monitor snapshot cache (30 s) could serve a stale
   pre-settle snapshot, so the chip could persist far longer than the actual
   Azure transition.

A manual reload remounts the query and fetches immediately, which is why only a
refresh appeared to work.

## User-facing change

While any cluster is provisioning (ARM `provisioning_state` in
Creating/Starting/Stopping/Updating/Deleting) **or** a live in-browser
start/stop/delete transition is tracked, the AKS cluster card now:

- polls every 5 s (capped below the user's chosen auto-refresh) instead of the
  idle 30–60 s cadence;
- keeps polling when the tab is backgrounded and refetches the moment the tab
  regains focus;
- asks the backend to bypass its 30 s monitor cache (`fresh=true`) on every
  poll — now extended to cover provisioning clusters started **outside** this
  browser (portal / CLI), which previously had no local transition record.

Once everything settles, the card automatically returns to the idle posture
(user's auto-refresh cadence, no background refetch, normal caching) — so idle
dashboards keep the calm, cost-minimised polling behaviour with no extra
background traffic.

## API / code change summary

- `web/src/components/cards/ClusterCard/ClusterCard.tsx`:
  - Added an `activePolling` state (seeded from the persisted-transition store)
    plus an `activePollingRef` mirror so the stable `queryFn` closure reads the
    current hot state at call time.
  - An effect drives `activePolling` from
    `hasProvisioningCluster || actions.transitioning.size > 0`.
  - While hot: `refetchInterval` is clamped to `min(base, 5_000)`,
    `refetchIntervalInBackground` / `refetchOnWindowFocus` are enabled,
    `staleTime` is `0`, and the `fresh` query flag is OR'd with
    `activePollingRef.current`.
  - Idle behaviour is unchanged (base interval, no background refetch, default
    stale time, `fresh` only when a local transition exists).

No backend, API contract, or IaC change. No new dependency.

## Validation

- `cd web && npx tsc` (via editor): no type errors in the changed file.
- `npx eslint src/components/cards/ClusterCard/ClusterCard.tsx`: clean.
- `npx vitest run src/components/cards/ClusterCard src/utils/aksStatus.test.ts`:
  21 passed.
- Diff isolated to `ClusterCard.tsx` (verified with `git diff --stat`).

