# Hide Managed Workspace Resource Groups

## Motivation

The Dashboard workspace discovery screen could show Azure-managed infrastructure resource groups such as `MC_...` and `ME_...` when those groups inherited ElasticBLAST tags. Those resource groups are owned by [AKS](https://learn.microsoft.com/azure/aks/intro-kubernetes) or [Azure Container Apps](https://learn.microsoft.com/azure/container-apps/overview) infrastructure and should not be selectable as BLAST workspaces.

## User-facing change

Workspace discovery, setup auto-fill, setup resource group pickers, and the header Workload RG picker now hide `MC_...` and `ME_...` managed resource groups.

## API/IaC diff summary

- No backend API or Bicep changes.
- Frontend managed-resource-group detection now treats both `MC_` and `ME_` prefixes as infrastructure-only groups.

## Validation evidence

- `cd web && npm run test -- src/lib/aksManagedRg.test.ts src/pages/Dashboard/configFromTags.test.ts`
- `cd web && npm run build`
- Removed inherited `elb-*` tags from the currently deployed `ME_...` resource groups and confirmed only `rg-elb-dashboard` and `rg-elb-dashboard-01` still carry `elb-*` workspace discovery tags.