---
title: Operate
description: Operator surface for ElasticBLAST Control Plane — deployment reference, architecture, and research notes that informed the design.
tags:
  - operate
---

# Operate

This tab is for **platform maintainers and administrators** who deploy, configure, monitor, or evolve the ElasticBLAST control plane on Azure. If you are a researcher driving the dashboard from a browser, the [User Guide](../user-guide/index.md) is the better entry point.

## Where to start

| Goal | Page |
|------|------|
| Deploy the control plane (manual `azd` flow, redirect URI, lockdown) | [Deployment Reference](../deployment-reference.md) |
| Pull latest code + build + roll out from a workstation (with snapshot + auto-rollback) | [CLI Rolling Update](cli-upgrade.md) |
| Understand the runtime topology, sidecars, network, identity | [Architecture → High Level](../architecture/high-level.md) |
| Dig into the shipped Azure Container Apps layout, cost, secrets | [Architecture → Container Apps](../architecture/container-apps.md) |
| Inspect the MSAL + managed-identity flow and the full RBAC matrix | [Architecture → Authentication](../architecture/authentication.md) |
| Read the in-progress research notes that informed BLAST result fidelity | [Research Notes](../architecture/index.md#research-notes-in-progress) |

## What lives here

- **[Deployment Reference](../deployment-reference.md)** — Bicep modules, `azd` workflow, post-provision steps, AKS sizing, six-sidecar template swap.
- **[CLI Rolling Update](cli-upgrade.md)** — the `git pull` + build + rolling-update wrapper around `quick-deploy.sh` / `postprovision.sh` for non-browser upgrades and emergency rollback.
- **Architecture** — `docs/architecture/` — the durable system map, container app topology, and authentication design.
- **Research Notes** — `docs/research/` — investigations that informed BLAST search-space + Web BLAST compatibility decisions. *Not user-facing documentation.*

For day-to-day researcher workflow, see the [User Guide](../user-guide/index.md). For codebase-level orientation (agent reference, repo layout), see the [Contributor](../contributor-guide/index.md) tab.
