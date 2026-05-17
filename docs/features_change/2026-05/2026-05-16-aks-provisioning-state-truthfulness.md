# AKS Provisioning State Truthfulness

## Motivation

AKS can report `powerState=Running` while the ARM resource is still `provisioningState=Creating`. The dashboard was treating that as a ready cluster, which made a freshly-created cluster look runnable too early and enabled cluster database status reads before Kubernetes was ready.

## User-Facing Change

The dashboard now treats a cluster as workload-ready only when `provisioningState=Succeeded` and `powerState=Running`. During creation, the cluster row shows the provisioning state instead of `Running`, and cluster-dependent database/warmup status is held back until provisioning completes. Storage database inventory is labelled as downloaded rather than ready, so it is not confused with node-local warmup readiness.

## API / IaC Diff Summary

- Added a frontend AKS lifecycle helper used by the dashboard, BLAST submit validation, warmup polling, and readiness gating.
- No backend API or IaC changes.

## Validation Evidence

- `npm run test -- src/utils/aksStatus.test.ts` passed: 3 tests, including `power_state=Running` + `provisioning_state=Creating` not being workload-ready.
- `npm run build` passed (`tsc -b && vite build`).
- Direct API check showed the newly-created cluster has now reached `provisioning_state=Succeeded` and `power_state=Running`.
- Browser reload at `http://127.0.0.1:8090/` showed the dashboard connected to the live cluster and Storage DB inventory as `5 downloaded`, `1 update`, `5/9 catalog` with the five database names.