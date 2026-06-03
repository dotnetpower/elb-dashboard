# Cluster card no longer flaps "Starting → stopped" after Start

## Motivation

Clicking **Start** on a stopped AKS cluster showed `Starting…` for a moment,
then the row flapped back to `Cluster is stopped`, and only a manual page
refresh showed the correct `Starting…` / provisioning state. This made the
Start action feel broken even though the cluster was actually starting.

## Root cause

The optimistic "starting" transition chip in
`web/src/components/cards/ClusterCard/useClusterActions.ts` was cleared the
instant the cluster list reported `power_state === "Running"`. AKS flips
`power_state` to `Running` at the very start of the start LRO while
`provisioning_state` stays `Starting`, and the backend's ~90s cluster-health
cache can still be serving the pre-start `Stopped` / `Succeeded` snapshot. So
the chip was dropped too early and the row fell back to the stale `Stopped`
snapshot, rendering `Cluster is stopped`. A refresh "fixed" it only because by
then the backend reported `provisioning_state === "Starting"`.

## User-facing change

The optimistic `Starting…` / `Stopping…` chip now persists until the cluster
has **settled** into its target state (`power_state` target reached **and**
`provisioning_state === "Succeeded"`), so the row no longer flaps to
`Cluster is stopped` during the start/stop transient. The existing 10-minute
transition deadline still evicts a genuinely stuck task.

## Code change summary

- `useClusterActions.ts`: extracted a pure, exported `transitionTargetReached()`
  predicate and changed the transition-clearing effect to require
  `provisioning_state === "Succeeded"` (via `isAksProvisioned`) in addition to
  the target `power_state`, for both `starting` and `stopping`.
- Added `transitionTargetReached.test.ts` covering the transient `Running +
  Starting`, stale `Stopped + Succeeded`, settled, and `deleting` cases.

No API or IaC changes.

## Validation

- `npx vitest run src/components/cards/ClusterCard/transitionTargetReached.test.ts
  src/utils/aksStatus.test.ts` → 11 passed.
- `npx eslint` on the changed files → clean.
- `npm run build` → green.
