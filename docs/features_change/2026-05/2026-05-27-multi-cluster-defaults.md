# Multi-cluster fleet defaults across the SPA

## Motivation

Several SPA surfaces fell back to `clusters[0]` (or RG-anchor match) when picking
which AKS cluster to drive a card, page, or settings action. With multi-tier
fleets (heavy/light/gpu) where some clusters are routinely Stopped, this
silently landed on the wrong cluster:

- BLAST jobs disappeared from the Jobs list because the chosen Stopped cluster
  had no matching `cluster_name` rows.
- Storage DB topology card advertised warmup capacity that did not exist.
- API Reference rendered the "Cluster is stopped" state even when the OpenAPI
  service was running on a healthy peer.
- Settings → AKS Observability was RG-scoped (missed clusters in
  `elasticblast-*` RGs entirely) and forwarded the workspace anchor RG to the
  backend, causing `cluster_not_found` failures for enable/disable.

## User-facing change

Same backing rule everywhere: **prefer a workload-ready cluster (Running +
Succeeded) over any positional fallback**. Each affected surface keeps its
caller-specific tie-breakers (name pin, RG hint, has-nodes preference) but the
"any Running cluster" line is always before the "first cluster" line.

Affected:
- BLAST Jobs list (`useScopedBlastJobs`).
- Storage card DB topology (`StorageCard`).
- API Reference page (`apiReference/clusterContext` + `ApiReference.tsx` + the
  prefetch hook) — already aligned in WIP, now backed by the shared helper.
- Settings → AKS Observability (`SettingsPanel` `AksSection`): switched to
  subscription-wide discovery, defaults to a Running cluster, dropdown shows
  `name (power_state)`, and enable/disable/status forward the **cluster's
  actual `resource_group`** instead of the workspace anchor RG.

Manual picks remain sticky for the rest of the page lifetime — the helper only
runs while the dropdown is empty / pointing at a deleted cluster / pointing at
a Stopped cluster when a Running peer exists (per consumer-specific rule).

## API / IaC diff summary

Pure frontend. No new HTTP routes, no Bicep changes. Backend
`/api/settings/aks-observability/*` contract is unchanged — the SPA simply
sends the correct `resource_group` now.

## Validation evidence

- New helper [web/src/utils/clusterSelection.ts](../../../web/src/utils/clusterSelection.ts) with
  [unit tests](../../../web/src/utils/clusterSelection.test.ts) (8/8 pass).
- Full SPA suite: `cd web && npm test -- --run` → 376/376 pass (51 files).
- Type-check: `npx tsc -p tsconfig.json --noEmit` clean.
- Lint: `npx eslint` on all touched files clean.

## Files touched

- `web/src/utils/clusterSelection.ts` (new) — `pickPreferredCluster(clusters, opts)`.
- `web/src/utils/clusterSelection.test.ts` (new).
- `web/src/components/SettingsPanel.tsx` — AKS Observability section: sub-wide
  fetch, store full cluster objects, default via helper, forward each cluster's
  actual RG to enable/disable/status calls, dropdown shows power_state.
- `web/src/components/cards/StorageCard.tsx` — topology picker via helper.
- `web/src/hooks/useScopedBlastJobs.ts` — discovered cluster via helper.
- `web/src/pages/apiReference/clusterContext.ts` (pre-existing WIP) — backed by
  the new helper.
