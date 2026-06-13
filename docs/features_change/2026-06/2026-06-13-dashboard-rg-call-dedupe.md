# Dashboard resource-group call dedupe

## Motivation

A live timing capture of the Dashboard first paint (browser Resource Timing API)
showed `GET /api/arm/.../resource-groups` being fetched twice on load, each a
~1–1.5 s ARM round trip competing for the single `api` sidecar. The two readers
used different React Query cache keys for the same upstream call, so TanStack
Query could not dedupe them:

- the `ResourcePicker` "Workload RG" dropdown fetched under `["arm-rgs", sub]`
  (inside an inline fetcher that maps rows to its own `Item[]` shape);
- `ClusterCard` fetched under `["arm", "resource-groups", sub]` for the raw
  resource-group rows it needs for the provision duplicate-name guard.

## User-facing change

No visible UI change. The duplicate resource-group request on Dashboard first
paint is collapsed into a single shared fetch, removing one redundant
~1–1.5 s ARM round trip from the initial load contention.

## Implementation

- New `web/src/api/resourceGroups.ts`: pins ONE canonical query key
  (`["arm", "resource-groups", sub]`) + a shared `RESOURCE_GROUPS_STALE_MS`
  (60 s) for the raw resource-group listing, and exposes an imperative
  `fetchResourceGroups(queryClient, sub)` that resolves through that key.
- `ConfigBar` and `DashboardHeader` picker fetchers now call
  `fetchResourceGroups` for the raw rows (then map to `Item[]` as before). Their
  picker OUTER key stays `["arm-rgs", sub]` on purpose — that entry caches the
  mapped `Item[]`, which must not collide with ClusterCard's raw
  `ArmResourceGroup[]` entry.
- `ClusterCard` now references the canonical key + shared stale constant instead
  of an inline `["arm", "resource-groups", sub]` / `60_000` pair, so all three
  readers resolve the same cache entry and share one in-flight request.

## API / IaC diff

None. Frontend-only React Query cache-key coordination; no backend route,
contract, or Bicep change.

## Validation

- `web/src/api/resourceGroups.test.ts` (new, 4 tests): canonical key shape,
  concurrent-caller dedupe into one upstream call, cache hit within the stale
  window, and separate fetches per subscription.
- `npx vitest run` on the changed components + new helper: 28 + 4 tests pass.
- `npm run build`: clean (TypeScript + bundling).
