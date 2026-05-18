# AKS Start Estimate Tips

## Motivation

Starting AKS takes several minutes, and automatic OpenAPI deployment plus database warmup can continue after the cluster power state flips to Running. Users need a lightweight estimate and useful waiting guidance instead of a static `Starting...` label.

## User-facing change

While a cluster is starting, the dashboard now shows a rotating estimate panel based on the latest observed startup timing:

- AKS start: about 235 seconds.
- OpenAPI deployment: about 31 seconds.
- Auto warmup: estimated from the number of selected Auto warm databases.

The panel rotates through practical tips about API readiness, warm cache timing, what is happening behind the scenes, and what the user can do while waiting.

## API / IaC diff summary

No API or IaC changes. This is a frontend-only dashboard status improvement.

## Validation evidence

- `cd web && npm run build` -> passed.
- `npx eslint src/components/ClusterItem/StartEstimatePanel.tsx src/components/ClusterItem/ClusterItem.tsx --max-warnings 0` -> passed.
- Full `npm run lint` still reports two pre-existing warnings outside this change: `src/pages/apiReference/EndpointCard.tsx` and `src/pages/blastSubmit/useDbWithWarmupPlan.ts`.
- Built and pushed `acrelbnm5virmqrdi5c.azurecr.io/elb-frontend:20260518045500-start-estimates`.
- Deployed to Container App revision `ca-elb-control--0000051`; `/api/health` and `/` both return HTTP 200.
- Restored ACR network posture after the build: `publicNetworkAccess=Disabled`, `defaultAction=Deny`.
