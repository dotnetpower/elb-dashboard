# Cluster card: initial skeleton only

## Motivation

The AKS cluster card could return to its animated list skeleton after a
failed first load when React Query retried or refreshed the cluster query
without any successful data yet. That made the card feel like it was
starting over repeatedly, and the error state could be mixed with the
empty-cluster call to action.

## User-facing change

* The cluster list skeleton now appears only for the first unresolved
  cluster-list request.
* After the first request has settled, later refreshes keep the current
  success or error content visible while the header refresh indicator shows
  background activity.
* The empty-cluster message is suppressed while the cluster query is in an
  error state, but the add-cluster call to action remains available so users
  can still provision a cluster from the failed-load state.

## API / IaC diff summary

* [web/src/components/cards/ClusterCard/ClusterCard.tsx](../../../web/src/components/cards/ClusterCard/ClusterCard.tsx)
  now derives an initial-only loading state from React Query's
  `dataUpdatedAt` / `errorUpdatedAt` timestamps and uses that state for
  the body skeleton, empty-state gates, and error-state add-cluster action.
* No backend, API contract, or IaC changes.

## Validation

* `npm run build` (in `web/`) — green. Vite emitted the existing
  Application Insights Rollup annotation warnings and chunk-size warning.