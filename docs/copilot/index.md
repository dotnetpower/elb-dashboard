---
title: Agent Reference
description: Durable knowledge for AI agents (Copilot) and human contributors editing ElasticBLAST Control Plane — codebase map, auth flow, browser terminal, version management, and more.
tags:
  - agent
---

# Agent Reference

This section is the agent-facing handbook for **ElasticBLAST Control Plane**. The pages were extracted from `.github/copilot-instructions.md` so the always-loaded charter stays small while the deep references remain searchable.

> Audience: AI coding agents (GitHub Copilot, Claude, Cursor) and new human contributors who need fast orientation in the codebase.

## Pages

| Page | When to read |
|------|--------------|
| [Codebase Map](codebase-map.md) | Quick "where does X live" lookup across `api/`, `web/`, `infra/`, `terminal/`. |
| [Repo Layout](repo-layout.md) | Full directory tree + edit-boundary table. |
| [Auth Flow](auth-flow.md) | MSAL + managed identity request lifecycle. |
| [Browser Terminal](browser-terminal.md) | `terminal` sidecar lifecycle, image, exec contract. |
| [Resource Plane](resource-plane.md) | Celery task table mirroring `azure-prereq.md`. |
| [Monitoring UI](monitoring-ui.md) | Dashboard card spec. |
| [Glass UI](glass-ui.md) | Glassmorphism CSS tokens and accessibility rules. |
| [Version Management](version-management.md) | `bump-version.sh` policy and SPA header stamp pipeline. |
| [Security Audit Follow-up](security-audit-followup.md) | Open design items from the 20-finding security sweep. |

## Related

- Full charter: [.github/copilot-instructions.md](https://github.com/dotnetpower/elb-dashboard/blob/main/.github/copilot-instructions.md)
- Navigation map: [AGENTS.md](https://github.com/dotnetpower/elb-dashboard/blob/main/AGENTS.md)
