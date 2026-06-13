---
title: MessageFlow constellation hardening (a11y, hover, perf, bounds)
description: Twenty critique-driven fixes to the D3 MessageFlow constellation — accessibility, hover correctness, performance, and visual bounds.
tags:
  - ui
---

# MessageFlow constellation hardening

## Motivation

A self-critique pass over the new D3 "Bounded Lanes (A1)" constellation
([MessageFlowConstellation.tsx](../../../web/src/components/cards/MessageFlow/MessageFlowConstellation.tsx))
surfaced a set of correctness, accessibility, and performance defects that the
mechanical review (build + focused tests) could not see. None changed the data
contract; all are presentation-layer hardening.

## User-facing change

Twenty fixes, grouped:

**Accessibility**
1. `svg role="img"` → `role="group"` so the focusable job buttons inside are no
   longer hidden from screen readers (an `img` collapses its subtree).
2. Decorative layers (boundary, labels, links, session hulls) marked
   `aria-hidden` so assistive tech only walks the interactive job nodes.
3. Dynamic `svg` `aria-label` summary ("N producers, M active jobs, K clusters").
4. Producer nodes get `role="img"` + `aria-label` (kind, alias, job count).
5. Consumer/cluster nodes get `role="img"` + `aria-label` (name, running, queued).
6. Producer `<title>` with the full untruncated alias + active count.
7. Cluster `<title>` with the full untruncated cluster name.

**Hover / interaction correctness**
8. Cluster-hover no longer dims the whole graph (clusters share a sentinel
   alias; hovering one must not set it as the active submitter).
9. Hover state is re-applied after a 20 s refetch rebuild so an in-progress
   hover survives the rebuild.
10. A hovered alias that left the snapshot is dropped before re-apply, so a
    departed submitter can never pin the graph into an all-dimmed state.
11. Drag gains a `clickDistance(5)` so a tiny jitter while clicking a job no
    longer suppresses opening its JSON detail (and a real drag no longer
    accidentally opens it).

**Performance**
12. `requestAnimationFrame`-debounced `ResizeObserver` so a drag-resize burst
    coalesces into a single rebuild instead of restarting the simulation per
    pixel.
13. Per-tick session bounding-box pass uses a precomputed `alias → members`
    map (O(jobs) instead of O(sessions × jobs)).
14. Live `prefers-reduced-motion` listener: flipping the OS setting rebuilds
    and immediately honours it (no remount needed).

**Visual bounds / robustness**
15. Job radius capped at 18 px so a pathologically large query cannot grow a
    node to fill the broker region.
16. Producer alias labels truncated (22 chars) so long UPNs do not run off the
    SVG edge.
17. Cluster name labels truncated (18 chars).
18. Session labels truncated (18 chars).
19. Removed dead `anchorX` / `anchorY` node fields.
20. Model unit test extended with the radius-cap case; the existing sqrt-scaling
    assertion moved to a sub-cap input.

## API / IaC diff summary

None. No backend, route, schema, or IaC change — the component still consumes
the same read-only `MessageFlowSnapshot`.

## Validation evidence

- `cd web && npm run build` — clean (tsc + vite).
- `cd web && npx eslint src/components/cards/MessageFlow/` — clean.
- `cd web && npx vitest run` — 852 passed (96 files), including the extended
  `constellationModel.test.ts` (radius cap + sqrt scaling).
