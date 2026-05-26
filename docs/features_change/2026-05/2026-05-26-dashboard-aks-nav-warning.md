# Dashboard AKS navigation warning

## Motivation

The top navigation showed the [AKS](https://learn.microsoft.com/azure/aks/intro-kubernetes) readiness warning dot and tooltip on **New Search**, even though the warning describes cluster health that belongs to the Dashboard monitoring surface.

## User-facing change

When no workload cluster is ready, the orange warning dot and hover tooltip now appear on **Dashboard**. **New Search** remains a plain navigation entry instead of carrying the cluster-health warning.

## API/IaC diff summary

No API or IaC changes. This is a React navigation presentation change in `web/src/components/Layout.tsx`.

## Validation evidence

`cd web && npm run build` passed on 2026-05-26.