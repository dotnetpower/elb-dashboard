# Cluster Health And Node Details

## Motivation

The dashboard cluster card could show `Degraded` when the AKS cluster itself was healthy, because API p95 latency, API errors, or a recent failed BLAST job were promoted to the top-level cluster health pill. The card also hid the node-detail entry point behind a secondary `Show details` / `Hide details` toggle, making the node breakdown feel missing.

## User-facing change

- The cluster health pill now reflects AKS/workload health signals: provisioning failure, node readiness or pressure, and very high CPU/memory pressure.
- API latency/errors and failed jobs remain visible in their own KPI/job cells but no longer mark the whole cluster as `Degraded`.
- The `Show details` / `Hide details` toggle was removed.
- Operational detail rows, including the node breakdown launcher, are visible whenever the cluster is workload-ready.
- The node breakdown action now reads `Node details`.

## API / IaC diff summary

Frontend only. No API or IaC changes.

## Validation evidence

- `cd web && npm run build` passed.