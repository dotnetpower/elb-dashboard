# New Search Cluster Readiness

## Motivation

The top navigation `New Search` item could warn that no AKS cluster was provisioned while the dashboard already showed an ElasticBLAST-managed cluster in another resource group.

## User-facing change

The `New Search` navigation guard now uses the same subscription-wide AKS discovery scope as the dashboard cluster card and the New Search cluster picker.

## API/IaC diff summary

No API or IaC changes. The frontend now calls the existing subscription-wide `/api/monitor/aks` query from the shared cluster readiness hook.

## Validation evidence

- `npm run test -- usePrerequisites useLatestBlastJob clusterContext`
- `npm run build`