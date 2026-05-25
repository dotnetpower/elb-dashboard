# Settings Sizing Recommendation

## Motivation

The six-sidecar Container App layout must stay on Azure Container Apps Consumption aggregate CPU and memory pairs. Operators need a quick way to see whether the current control-plane size is under pressure before editing the Bicep resource requests and redeploying.

## User-facing change

The Settings panel now includes a Control Plane Sizing section. It reads the existing live sidecar metrics stream and shows the current aggregate Consumption pair, the next valid scale step, per-sidecar CPU and memory pressure, and a sizing recommendation. Scale-up recommendations require repeated pressure samples so short spikes are shown as watch states instead of immediate scale-up advice.

## API/IaC diff summary

- No new backend route was added; the UI reuses the existing `/api/monitor/sidecars` snapshot and SSE cache path exposed by `useSidecarMetrics`.
- The sizing model reflects the current six-sidecar Bicep requests: `2.25` CPU / `4.5Gi` memory total.
- Recommendations are read-only and do not mutate Azure resources.
- Single-sample pressure is downgraded to a watch state until the same sidecar reports scale-level pressure across at least three recent samples.

## Validation evidence

- `cd web && npx tsc --noEmit` passed.
- `cd web && npm run build` passed with existing Rollup annotation/chunk-size warnings.