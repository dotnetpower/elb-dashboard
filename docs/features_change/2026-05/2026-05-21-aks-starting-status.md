# AKS starting status indicator

## Motivation

AKS can report `provisioning_state=Starting` while `power_state=Running` during a cluster start. The dashboard treated this as not workload-ready but did not classify it as an in-progress lifecycle state, so the cluster row could show the stopped readiness copy instead of a starting indicator.

## User-facing change

The dashboard now treats AKS `Starting` and `Stopping` provisioning states as transitioning. Cluster rows show the existing animated transition state and status label while Azure is still completing the lifecycle operation.

The dashboard Workload RG picker also keeps AKS-managed `MC_...` node resource groups disabled and shows a saved custom RG value explicitly instead of letting the browser display the first option as if it were selected.

## API / IaC diff summary

No API or IaC changes. Frontend AKS status classification now includes `Starting` and `Stopping` alongside `Creating`, `Updating`, and `Deleting`. The compact resource picker now renders an explicit custom option when the saved value is not present in the fetched option list, and the dashboard header reuses the AKS-managed RG classifier for Workload RG options.

## Validation evidence

- `cd web && npm run test -- src/utils/aksStatus.test.ts`
- `cd web && npm run build`