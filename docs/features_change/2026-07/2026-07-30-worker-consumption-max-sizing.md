---
title: Worker sidecar uses the full Consumption resource envelope
description: Raise the worker sidecar to 1.75 vCPU and 3.5 GiB, synchronize the sizing UI, and guard quick deploy resource reconciliation across environments.
tags:
  - infra
  - operate
---

# Worker sidecar uses the full Consumption resource envelope

## Motivation

The bundled [Azure Container Apps](https://learn.microsoft.com/azure/container-apps/overview)
replica runs one API process and a worker topology with three Celery parents,
five prefork children, and a resident Service Bus consumer. Production evidence
previously required raising the worker from 2 GiB to 3 GiB after in-flight
allocations still triggered child OOM kills despite post-task recycling.
Service Bus bursts add up to four concurrent submit handlers, so the remaining
Consumption-profile capacity is assigned to the worker as memory and CPU
headroom.

The Settings sizing table also retained the older `1 vCPU / 2 GiB` worker
value. That made the displayed limit and aggregate pair disagree with both the
Bicep template and the live cgroup percentage.

## User-facing change

- The worker sidecar allocation is `1.75 vCPU / 3.5 GiB`.
- The bundled replica total is exactly `4 vCPU / 8 GiB`, the Consumption-profile
  maximum. Further vertical growth requires moving capacity from another
  sidecar or selecting a dedicated workload profile.
- Settings → Control Plane Sizing displays the worker's current allocation and
  reports the aggregate as `4 CPU / 8.0Gi`.
- Fast deployments continue to read each container's desired resources from
  the checked-out Bicep template. Running `quick-deploy.sh api`, `worker`, or
  `all` in another environment therefore converges the worker to the same
  allocation instead of preserving a stale live value.

## API and IaC summary

- `infra/modules/containerAppControl.bicep` raises worker CPU/memory and updates
  the six-sidecar aggregate contract.
- `web/src/components/settings/sections/SizingSection.tsx` synchronizes the
  worker limit used for normalization and display.
- `api/tests/test_sidecar_resource_contract.py` verifies all six Bicep resource
  pairs, the exact Consumption aggregate, UI parity, execution of the real
  quick-deploy resource parser, and resource reconciliation in both PATCH
  paths.
- Generated ARM JSON is rebuilt from the Bicep sources.

No Service Bus concurrency, Celery pool size, Redis policy, role assignment,
network rule, or sidecar count changes.

## Validation evidence

- `uv run pytest -q api/tests/test_sidecar_resource_contract.py`
- `bash -n scripts/dev/quick-deploy.sh`
- `az bicep build --file infra/modules/containerAppControl.bicep`
- `az bicep build --file infra/main.bicep`
- `cd web && npm run build`

Deployment was intentionally not run. The operator will apply the revision with
`scripts/dev/quick-deploy.sh`; that path is covered by the resource contract
test above.
