---
title: Monitoring UI (Agent Detail)
description: Card-by-card specification of the ElasticBLAST Control Plane Dashboard — readiness signals, degraded-state semantics, and the api/routes/monitor backend contract.
tags:
  - agent
  - ui
---

# Monitoring UI (detail)

> Extracted from `.github/copilot-instructions.md` §8 on 2026-05-19.

The dashboard is the landing page; the Browser Terminal is one tab among many.

Required cards (each backed by a polled REST endpoint, 30 s default refresh):

1. **Cluster** — AKS name, RG, region, K8s version, node pool size/SKU, `powerState`, `provisioningState`, kubelet identity object id, attached ACR.
2. **Storage** — account name, region, public-access state (read-only indicator; should always show **Disabled** for the workload account), container list, blob counts/sizes for `blast-db/`, `queries/`, `results/`.
3. **ACR** — registry, login server, repositories with tag table (highlight mismatches against `IMAGE_TAGS`).
4. **Jobs** — list of ElasticBLAST submissions with status (`Provisioning | Downloading DB | Splitting | Running | Completed | Failed | Deleted`), elapsed time, results URL. Drill-down opens the Celery task's full event history from Table Storage.
5. **Browser Terminal** — `terminal` sidecar process state, last `az login` heartbeat (mtime of `~/.azure/azureProfile.json` on the `terminal-home` share), button to open the embedded shell.
6. **Container App** — revision name, image digests for each sidecar, replica count (always 1), CPU/memory % per sidecar pulled from App Insights.

All numbers must come from real Azure / Kubernetes APIs. Never fabricate or cache stale data without showing a "last refreshed" timestamp.
