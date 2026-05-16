# 2026-05-16 — AKS card information density pass

## Motivation

After the first v3 dashboard rollout the AKS card carried three problems
the user surfaced explicitly during review:

1. The card body **duplicated** the per-node table that lives in the
   modal's Cluster Diagnostics section, but used raw `m`/`Mi` units that
   non-K8s operators can't read at a glance (`257734Mi` for an `E32s_v5`
   is hard to recognise as ~252 GiB).
2. Three nodes in the same VMSS were rendered with **identical** truncated
   names because the `replace(/-vmss\d+$/, '')` rule stripped the only
   characters that disambiguated them.
3. **Three "OK" signals** (top-right `OK` chip + cluster-name `Running`
   chip + STATE `Succeeded` pill) competed for attention without
   conveying additional meaning in the steady-state.

Eight smaller items (#5..#11 in the analysis) were folded into the same
pass to avoid a second round-trip.

## User-facing change

### Card body (the always-visible part)

* **Replaced** the per-node table with a one-line **summary strip** that
  reads:
  ```
  ● 1 (system) · ● 3 (user) · 4 NODES
   · CPU 0.52 / 98.0 cores (0.5%)
   · MEM 4.88 / 763 GiB (0.6%)
   · ● all Ready
  ```
  - Pool dots colour-code system (warning/orange) vs user (accent/blue).
  - Cores and GiB values are humanised (modal already had this; card now
    matches).
  - Health flag on the right swaps to `1 NotReady` (danger pill),
    `MemoryPressure` (warning pill), or `1 hot` (warning) when the
    aggregate detects unhealthy/saturated nodes.
  - The whole strip is clickable and opens the modal for the per-node
    breakdown — preserving the drill-down without rendering the rows
    twice on the dashboard.
* `View full details` button kept for keyboard / screen-reader access.

### Cluster header row

* **Workspace ready** chip moved from its own standalone strip into the
  same row as the cluster name and power state. The "is this cluster
  usable?" answer now lives in one place.
* **Stop / Delete** buttons are now grouped at the far right of the row,
  separated from the cluster name by a thin vertical divider, so
  destructive actions don't sit shoulder-to-shoulder with metadata.
* The standalone "Workspace ready" strip is removed; the per-database
  warmup chip strip remains so progress is still visible while DBs are
  loading.

### STATE pill

* Hidden when ARM provisioning is the steady `Succeeded` state — the
  `Running` chip already conveys "the cluster is fine". Still rendered
  (with the appropriate accent / warning / danger pill) for `Creating`,
  `Updating`, `Deleting`, and `Failed`.

### SHARD CAPACITY block

* Rewritten as a single line: `Shard capacity · 3 nodes ·
  Standard_E32s_v5 · auto-sized per submit (max 10 jobs)`.
* `target N ≤ 10` jargon replaced with `max 10 jobs`; the sentence about
  ARM throttling lives in the explanatory `title` tooltip.

### Pool cards

* Each pool card now carries a footer line with a per-node and total
  capacity readout, e.g.:
  ```
  SYSTEM   1 × Standard_D2s_v3
  2 cores · 8 GiB / node
  ```
  ```
  USER     3 × Standard_E32s_v5
  32 cores · 256 GiB / node · 96 / 768 GiB total
  ```
  Total only renders when the pool has more than one node (otherwise it
  would just repeat the per-node value).

## API / IaC diff summary

Frontend only — no backend or Bicep changes.

| File | Change |
|------|--------|
| [web/src/components/ClusterDetailModal.tsx](../../../web/src/components/ClusterDetailModal.tsx) | `ClusterDetails` body table replaced with a memoised aggregate summary strip. Helpers `isSystemPool`, `fmtCores`, `fmtGiB` colocated. Imports trimmed (`Maximize2`, `AlertTriangle`, `Copy`, `Loader2`, `X` only). |
| [web/src/components/ClusterItem.tsx](../../../web/src/components/ClusterItem.tsx) | `useAksSkus` consumed for pool capacity totals. `Workspace ready` chip moved next to power label. Stop/Delete grouped on the right with a divider. STATE pill hidden when `Succeeded`. Shard-capacity block reflowed to one line with friendlier wording. |

No new dependencies, no CSS changes (existing `dv3-pool-card .footer`,
`dv3-warmup-chip`, `dv3-shard-capacity` tokens are reused).

## Validation evidence

* `npx tsc --noEmit` (in `web/`) → clean.
* `npm run build` (in `web/`) → success (671 kB JS / 89 kB CSS,
  unchanged chunk warning).
* `uv run pytest -q api/tests` → not re-run; backend untouched in this
  pass (the previous change note in this directory ran 219/219 already).
* Visual verification at <http://127.0.0.1:18080/>:
  * Card body renders the new one-line summary
    `● 1 ● 3 4 NODES · CPU 0.52 / 98.0 cores (0.5%) · MEM 4.88 / 763 GiB
    (0.6%) · all Ready`.
  * `Running 🔥 ready` chips sit next to the cluster name; Stop/Delete
    are right-aligned behind a divider.
  * No redundant `STATE Succeeded` pill.
  * Pool cards show `32 cores · 256 GiB / node · 96 / 768 GiB total`.

## Out of scope

* `#9 — cascade pressure to the cluster card header dot` is partially
  addressed by the `NotReady` / pressure / hot pill in the new summary
  strip. A full propagation up to the cluster name row would require
  hoisting the `topQuery` from `ClusterDetails` into `ClusterItem`. We
  decided that the prominent in-strip pill is enough for now; revisit
  if a real incident shows operators missed it.
* `#8 — cross-pool memory % comparability hint` is moot at the card
  level now that the body shows a single cluster-wide aggregate. The
  per-pool grouping inside the modal already distinguishes system vs
  user nodes by colour stripe.
