---
title: Results page scoping uses the job's own cluster RG, not the workspace anchor
description: Fixes BLAST Results listing, download, export, and cancel targeting the workspace anchor resource group instead of the cluster the job actually ran on, which broke cross-RG multi-cluster fleets.
tags:
  - blast
  - ui
  - aks
---

# Results page scoping uses the job's own cluster RG, not the workspace anchor

## Motivation

The AKS cluster picker is **subscription-wide**, so a BLAST job can run on a
cluster that lives in a different resource group than the workspace anchor RG
(`config.workloadResourceGroup`). On the deployed target the dashboard anchor is
`rg-elb-dashboard` while clusters live in `rg-elb-cluster` (`elb-cluster-01`,
`elb-cluster-02`).

The Recent Searches → Results page derived its Azure scope
(`subscriptionId` / `storageAccount` / `resourceGroup` / `clusterName`) from the
submit payload, then fell back to the workspace **anchor** config when a field
was absent:

```ts
const resourceGroup =
  searchParams.get("resource_group") ||
  payloadResourceGroup ||
  config?.workloadResourceGroup ||  // BUG: anchor RG, not the job's cluster RG
  "";
const clusterName =
  searchParams.get("cluster_name") || payloadClusterName || "elb-cluster";
```

For legacy jobs (or any job whose payload omits these fields) the page then
queried the **wrong** resource group / cluster. Because that `resourceGroup`
feeds `blastApi.listResults`, `downloadResult(File)`, `exportResults`, and
`cancelJob`, every one of those operations failed for a cluster that did not
happen to sit in the anchor RG.

This is the same anchor-RG-vs-cluster-RG bug class already fixed in
[BlastSubmit.tsx](../../../web/src/pages/BlastSubmit.tsx) and
[useScopedBlastJobs.ts](../../../web/src/hooks/useScopedBlastJobs.ts). A sweep of
the other menus confirmed **Terminal** (stateless sidecar, no cluster/RG
selection) and the **API Reference** OpenAPI executor
([clusterContext.ts](../../../web/src/pages/apiReference/clusterContext.ts),
[useOpenApiExecutor.ts](../../../web/src/hooks/useOpenApiExecutor.ts)) were
already correct; the Results page was the only remaining offender.

## User-facing change

On the BLAST Results page, results listing, file download, report export, and
job cancel now target the cluster the job **actually ran on**. Jobs whose
cluster lives outside the workspace anchor resource group are addressed
correctly instead of silently failing.

## API / code change summary

- New pure helper [web/src/pages/blastResults/blastJobScope.ts](../../../web/src/pages/blastResults/blastJobScope.ts)
  (`resolveBlastJobScope`) resolves the four scoping identifiers in priority
  order: URL query params → submit payload → `job.infrastructure` (authoritative
  backend record of where the job ran) → workspace anchor config (legacy last
  resort). Inserting `job.infrastructure.*` ahead of the anchor config is the
  fix.
- [web/src/pages/blastResults/useBlastResultsState.ts](../../../web/src/pages/blastResults/useBlastResultsState.ts)
  now derives its scope through the helper instead of an inline fallback chain;
  the dead local `stringFromPayload` helper was removed.
- The downstream consumers — `useBlastResultActions` (download / export /
  cancel) and `StorageLockedPanel` (unlock) — inherit the corrected RG/cluster
  automatically; no change was needed there.
- No backend, IaC, or sidecar changes. Frontend-only.

## Validation

- `npx vitest run src/pages/blastResults/blastJobScope.test.ts` — 6 new tests
  cover the cross-RG infrastructure fallback, URL/payload precedence, the legacy
  `aks_cluster_name` alias, and the anchor-config last resort.
- `npm test -- --run` — 474 tests pass (61 files).
- `npm run build` — clean production build.
- `npx eslint` on the three touched files — clean.
