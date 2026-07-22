# Sidecar topology card shows absolute memory when no cgroup limit

## Motivation

On the dashboard, the sidecar topology card rendered `mem —` for every
sidecar. Container Apps sidecars run without a cgroup `memory.max` limit, so the
`cgroup_reporter` cannot compute a percentage and leaves `mem_pct` null. The
reporter still always writes the absolute `mem_bytes`, but the topology footer
only rendered `mem_pct`, so the memory telemetry looked missing.

## User-facing change

- The sidecar topology footer now shows the absolute memory (e.g. `mem 128M`)
  when a percentage is unavailable, instead of `mem —`.
- When a percentage is available it is still shown as `mem 55%`.
- A stale snapshot still renders `mem —` (both `mem_pct` and `mem_bytes` are
  cleared), so a frozen tile is not mistaken for live data.

## API / IaC diff summary

No API or IaC change. Frontend-only:

- `web/src/components/cards/SidecarsCard/helpers.ts` — new `memLabel()` +
  `formatMemBytesCompact()` helpers; `staleSnapshot()` now also clears
  `mem_bytes`.
- `web/src/components/cards/SidecarsCard/TopoNode.tsx` — footer uses
  `memLabel()`.
- `web/src/components/cards/SidecarsCard/helpers.test.ts` — new unit tests.

## Validation evidence

- `cd web && npm run build` — passes.
- `npx vitest run src/components/cards/SidecarsCard/helpers.test.ts` — 6 passed.
- `npx vitest run src/components` — 252 passed.
- `npx eslint` on the touched files — clean.
