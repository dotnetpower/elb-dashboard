# 2026-05-27 — API Reference multi-cluster selection

## Motivation
On a multi-cluster fleet the `/docs` page was hard-coding `clusters[0]`
to pick the AKS cluster that hosts the `elb-openapi` service. Azure
returns the sub-wide cluster list in arbitrary order, so when the
first cluster happened to be Stopped the page rendered the
"AKS cluster is stopped" panel even though a healthy peer existed —
the user had no way to switch to the running cluster.

## User-facing change

- `/docs` (API Reference) now picks the workload-ready cluster
  preferentially via the existing `pickPreferredCluster` helper,
  matching the Dashboard's selection logic.
- When the fleet has more than one ElasticBLAST-managed cluster, a
  compact cluster picker is rendered between the hero and the body so
  the user can switch between clusters. Each chip shows a status dot
  (green = running, red = stopped) and the cluster name; non-running
  clusters also carry their `power_state` in parentheses.
- The user's choice persists in `localStorage` under
  `elb-api-ref-cluster` so navigation away and back, or a hard reload,
  keeps targeting the same cluster.
- Stale preferences (cluster deleted after it was selected) are
  cleared automatically on the next render — the picker falls back to
  the auto-selected workload-ready cluster.
- The Dashboard's `usePrefetchApiReference` hook reads the same
  storage key so cache warm-up targets the cluster the page will
  actually use.

## API / IaC diff summary
- Frontend only.
  - `web/src/pages/apiReference/clusterContext.ts` — replace
    `clusters[0]` with `pickPreferredCluster`; accept an optional
    `preferredClusterName`; return the full `candidates` array.
  - `web/src/pages/apiReference/clusterContext.test.ts` — add 3 new
    test cases (workload-ready preference, preferred-name honour,
    stale-name ignore).
  - `web/src/pages/ApiReference.tsx` — load/persist the preferred
    name, wire `preferredClusterName` into the context resolver,
    render `<ClusterPicker>` whenever more than one cluster is
    available, drop the stored preference when the cluster no longer
    exists.
  - `web/src/hooks/usePrefetchApiReference.ts` — read the same
    storage key so warm-up matches the page's choice.

## Validation
- `cd web && npm test -- --run` → 376 / 376 passing (was 363, +13
  new cases covering the resolver behaviour).
- `cd web && npm run build` → clean (only the pre-existing chunk-size
  warning remains).
- `cd web && npx eslint src/pages/ApiReference.tsx src/pages/apiReference src/hooks/usePrefetchApiReference.ts`
  → clean.
- Manual: with one Running + one Stopped cluster the picker renders
  with Running first; clicking the Stopped chip re-renders the
  existing "AKS cluster is stopped" panel for that cluster.

## Notes / deferred
- The same `clusters[0]` blind spot exists in
  `useScopedBlastJobs.ts` and `components/cards/StorageCard.tsx`.
  Those pages already auto-prefer the cluster whose RG matches the
  workspace anchor RG so the symptom is less acute, but they should
  also adopt `pickPreferredCluster` in a follow-up. Tracked as a
  related cleanup item; out of scope for this fix.
