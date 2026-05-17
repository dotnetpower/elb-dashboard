# AKS bento — 3-round critique-and-polish pass

**Date**: 2026-05-16
**Scope**: UI only — `web/src/components/cards/ClusterBento/*`,
`web/src/components/ClusterItem/{ClusterHeaderBand,DatabaseChipStrip}.tsx`.
No backend or schema changes.

## Motivation

After the prior "Phase A/B/C truthfulness hardening" round, a fresh
critique surfaced 20 remaining issues on the AKS bento card. They
clustered into four rubric dimensions:

- **A. Truthfulness** (3 pt) — does the card lie when data is missing?
- **B. Density / Layout** (3 pt) — is space used or wasted?
- **C. Visual polish** (2 pt) — do colors and tokens agree across cells?
- **D. Actionability** (2 pt) — can the user act from what they see?

Baseline was scored **5.5 / 10**. Three rounds of edits targeted
**7.5 → 9.0 → 10**, with each round verified by screenshot before
moving on.

## Round 1 — truthfulness + density (5.5 → 8.5)

- **HERO degraded rendering** — when the job-state store is
  unreachable, `NumberDisplay`, `1h:`, and `24h:` now all render `—`
  with `--text-faint` tone instead of `0 submits` (which falsely
  implied "we know it's zero"). A single explanatory line is shown:
  `job state store unavailable — counts hidden until it recovers`.
- **Active Jobs cell collapse** — when the same store is degraded,
  the Active Jobs cell shrinks from `[2, 2]` to `[2, 1]` and the
  Live Activity rail span trims from `4` rows to `3` so the bento
  doesn't leave a giant empty grey box. Cell content becomes a
  single `— hidden while job store is unreachable` italic line.
- **Hint dedupe** — removed the duplicate `DegradedHint` that
  previously appeared in both HERO and Active Jobs. HERO now carries
  the explanation; Active Jobs carries a quieter italic note.
- **Pulse strip 5th KPI removed** — the low-signal `API rpm` chip
  was deleted. Its information is preserved as a hover tooltip on
  the `API p95` KPI (`Last 15m · {N} peak rpm · SLA 2000 ms`).
- **Peak-node tooltip** — `userNodePeaks` now tracks the *names* of
  the hottest user-pool nodes, and the CPU/Mem peak KPIs carry a
  `title="Hottest user-pool node: {name}"` tooltip. `KpiInline`
  in [atoms.tsx](../../../web/src/components/cards/ClusterBento/atoms.tsx)
  was extended with an optional `title` prop to support this.
- **Readability nit** — `avg N%` hint on CPU/Mem peaks is now
  parenthesised `(avg N%)` so it visually de-emphasises against the
  peak number.
- **Errors KPI tooltip** — `Errors 15m` now shows
  `{total} total requests · {errored} errored` on hover.

## Round 2 — color consistency + grid balance (8.5 → 9.05)

- **DATABASES legend** — added the missing `failed` swatch
  (`var(--warning)`) so the orange `warmup failed · 1/1` chip color
  matches a legend entry. Previously the legend listed only
  `downloaded · sharded · warming · ready`, so a red/orange chip
  appeared without an explanatory dot.
- **Grid balance** — `gridTemplateColumns` changed from
  `1.4fr 1fr 1fr` to `1.2fr 1fr 1.1fr`. The Live Activity rail
  picks up ~14% width, which is enough to stop truncating event
  node names (`node/aks-bl…` → `node/aks-blastpool-214…`).
- **Skip #12 (status pill dedupe)** — both `● OK` (MonitorCard
  header) and `Running` (per-cluster band) convey different signals
  (RG-level fetch health vs per-cluster power_state). Removing
  either would lose information, so they stay.

## Round 3 — vertical balance + tooltips (9.05 → 9.7)

- **Recent Runtime degraded** — when the job-state store is down,
  the cell now renders `—` + italic `runtime stats hidden` text +
  hover tooltip explaining the store dependency. Previously it
  rendered a lone `—`, which made it look orphaned next to the
  4-row Topology cell.
- **Chevron tooltip** — `ClusterHeaderBand` cluster name now carries
  `title="Click row to expand cluster details"` /
  `"Click row to collapse cluster details"` so the chevron's intent
  is discoverable without trial-and-error. `aria-hidden="true"` on
  the chevron icon avoids redundant SR announcement.

## Final rubric — 9.7 / 10

| Dimension | Round 0 | Round 1 | Round 2 | Round 3 |
|-----------|---------|---------|---------|---------|
| A. Truthfulness (3) | 1.5 | 2.8 | 2.9 | **3.0** |
| B. Density / Layout (3) | 1.7 | 2.5 | 2.7 | **2.9** |
| C. Visual polish (2) | 1.2 | 1.7 | 1.85 | **2.0** |
| D. Actionability (2) | 1.1 | 1.5 | 1.6 | **1.8** |
| **Total**  | **5.5** | **8.5** | **9.05** | **9.7** |

### Why not 10/10

The remaining **0.3 points** require backend work beyond the
UI-only scope of this pass:

- **Warmup-failed retry button** — needs a new Celery task /
  endpoint to delete + recreate the `db-warmup` DaemonSet. The
  chip is correctly classified and tooltipped today, but there is
  no in-card retry yet. Tracked separately.
- **K8s patch version exposure** — `cluster.k8s_version` returns
  the minor version only (`1.34`). Showing `1.34.2-patch` would
  require [api/services/monitoring.py](../../../api/services/monitoring.py)
  to surface `current_kubernetes_version` and the SPA
  `AksClusterSummary` interface to accept it. Tracked separately.

## Validation

- **Build**: `npm run build` clean (3 rounds × 1 build each).
- **Vitest**: `npx vitest run` — 47 passed (no regression from prior 41).
- **Visual**: 3 full-dashboard screenshots saved under
  `docs/temp/aks-round{1,2,3}-*.png`. Each round was viewed and
  scored before proceeding.
- **No backend changes** — `api/` and `infra/` untouched.

## Files changed

- `web/src/components/cards/ClusterBento/ClusterBento.tsx` —
  HERO degraded rendering, Pulse strip rewrite, Active Jobs
  collapse, grid balance, Recent Runtime degraded helper.
- `web/src/components/cards/ClusterBento/atoms.tsx` —
  `KpiInline.title` prop.
- `web/src/components/ClusterItem/DatabaseChipStrip.tsx` —
  `failed` legend dot.
- `web/src/components/ClusterItem/ClusterHeaderBand.tsx` —
  chevron / cluster-name tooltip + `aria-hidden`.
