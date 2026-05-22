# Workload RG stale config recovery

## Motivation

Switching local Azure contexts can leave the browser with an older `elb-resource-config` value. When the saved workload resource group no longer exists in the active subscription, the dashboard header rendered it as a custom resource group and continued polling the wrong scope.

## User-facing change

The Dashboard Workload RG picker now replaces a stale or disabled saved resource group with the first enabled resource group returned by the active subscription scope. Users no longer need to clear local storage manually after switching subscriptions or tenants.

## API/IaC diff summary

No API or IaC changes. Frontend-only behavior change in the resource picker and dashboard header.

## Validation evidence

- `cd web && npm run build`
- Browser check: seeded `elb-resource-config` with stale `workloadResourceGroup=rg-elb-01`, reloaded `http://localhost:8090/`, and confirmed the saved config was rewritten to `rg-elb-dashboard-01` with the matching ACR and Storage tag values.
