# 2026-05-27 — Multi-cluster Cluster Plane hardening

## Motivation
User review of the dashboard "Cluster Plane" surfaced 25 issues, most
notably that the per-cluster `API p95` / `Errors 15m` cells were
actually rendering the dashboard backend's own `/api/blast` ring-buffer
metric. The same value was therefore being repeated on every cluster
row and was driving misleading per-cluster "degraded" status lines.
Several related ergonomics gaps (no fleet KPI, no sort, no Portal
link, weak Delete confirmation, mobile users could not Start/Stop a
cluster) were addressed in the same pass.

## User-facing change

- **Correctness**
  - `/api/blast` request metrics are no longer rendered inside every
    cluster row. They moved to a single card-header strip labelled
    `Dashboard p95 N · M 5xx / 15m` with a tooltip making clear that
    the value is the dashboard backend's own metric, not the K8s API
    server / per-cluster signal.
  - `useClusterHealth` no longer mixes that global metric into the
    per-cluster verdict or its status line — the dot tone and the
    substatus line now always agree (a green dot can no longer be
    paired with a yellow substatus text).
  - The Pulse meta grid hides `CPU peak`, `Mem peak` and `DBs` when
    the cluster is stopped / transitioning / provisioning, instead of
    rendering empty `—` cells that look like "0%" / "0 visible".

- **Multi-cluster ergonomics**
  - Cluster rows are now sorted issues-first (failed → provisioning →
    stopped → not-ready → healthy, alphabetical within each bucket).
  - A fleet KPI strip above the row list reads
    `N clusters · X running · Y stopped · …` plus the dashboard p95
    summary.
  - Clusters missing the `elb-tier` ARM tag now show a dashed
    `untagged` chip instead of nothing, so the tagging gap is visible.

- **Actions / safety**
  - Every row carries an `Open in Portal` button that deep-links to
    the AKS resource overview in the Azure Portal.
  - Start / Stop / Delete are now reachable on mobile (the
    `dashboard-hide-mobile` class was removed).
  - The Delete dialog requires the user to type the cluster name
    exactly before the Delete button enables (extends `ConfirmDialog`
    with a generic `typeToConfirm` prop).
  - Start/Stop/Delete completion + error messages are now mirrored
    into a visually-hidden `aria-live="polite"` region so screen
    reader users hear them, and the visible banners also carry
    `role="status"`.

- **Copy / cleanup**
  - The third pulse stat is now labelled `Load` (max CPU/Mem peak)
    instead of the previously-undefined `Pressure`.
  - The "warming · ready · failed" Databases legend is now only
    rendered when there is actually at least one warm chip, so the
    "No warmed databases yet" empty state stays focused.

## API / IaC diff summary
- No backend, OpenAPI or Bicep change.
- Frontend only:
  - `web/src/components/cards/ClusterPulse/useClusterHealth.ts` — drop `apiP95`/`apiErrors` from inputs, drop the soft-degraded substatus branch.
  - `web/src/components/cards/ClusterPulse/usePulseSignals.ts` — drop the per-cluster `monitoringApi.requestMetrics` query; reuse `topQuery.isError` as the per-cluster `metricsDegraded` signal.
  - `web/src/components/cards/ClusterPulse/PulseMetaGrid.tsx` — drop `API p95` / `Errors 15m` cells; new `operational` prop gates `CPU peak` / `Mem peak` / `DBs` cells.
  - `web/src/components/cards/ClusterPulse/ClusterPulse.tsx` — pass `subscriptionId` + `operational` through, drop the lifted props.
  - `web/src/components/cards/ClusterPulse/PulseActions.tsx` — add `Open in Portal` link, drop `dashboard-hide-mobile`.
  - `web/src/components/cards/ClusterPulse/PulseRowSummary.tsx` — rename `Pressure` → `Load`; render `untagged` tier badge when the tag is missing.
  - `web/src/components/cards/ClusterCard/ClusterCard.tsx` — sort `clusters` issues-first, fleet KPI strip, lifted dashboard-metrics fetch, ARIA live region, name-typed Delete.
  - `web/src/components/ConfirmDialog.tsx` — new optional `typeToConfirm` / `typeToConfirmLabel` props with disabled-until-match guard.
  - `web/src/components/ClusterItem/DatabaseChipStrip.tsx` — hide the warming/ready/failed legend when `warmChips.length === 0`.

## Validation
- `cd web && npm test -- --run` → 363 / 363 passing.
- `cd web && npx tsc --noEmit` → clean.
- `cd web && npm run build` → clean (warns only on pre-existing chunk-size limit).
- `cd web && npx eslint src` → clean.

## Deferred (intentionally not in this PR)
- Pre-Start cost / time estimate in the row (#16): needs a backend
  cost-projection endpoint that does not exist yet.
- Provision queueing instead of strict single-in-flight (#14):
  architectural change to `useClusterProvisioning`; out of scope for a
  UX hardening pass.
- Per-row "last refreshed" timestamp (#20): card-footer already shows
  freshness; per-row would clutter the line without a clear win until
  rows can stale independently.
- Delta-only Region / K8s row (#13): cosmetic, debatable in a fleet
  where regions or K8s versions are heterogeneous.
