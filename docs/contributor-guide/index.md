---
title: Contributor Guide
description: How to contribute to ElasticBLAST Control Plane — repo conventions, documentation workflows, and the AI Agent Reference used by Copilot and coding agents.
tags:
  - contributor
---

# Contributor Guide

This guide collects everything you need to contribute code or documentation to **ElasticBLAST Control Plane**. If you only want to *use* the dashboard, start with the [User Guide](../user-guide/index.md) instead.

## Pages

- [Screenshot Workflow](screenshot-workflow.md) — capture and refresh product screenshots used across the docs.
- **Agent Reference** — durable knowledge for AI agents (Copilot) and humans editing the codebase. See the index page below.

## Agent Reference

The `copilot/` section is the agent-facing handbook extracted from `.github/copilot-instructions.md`. It also serves as the deep-codebase orientation for new human contributors.

- [Codebase Map](../copilot/codebase-map.md)
- [Repo Layout](../copilot/repo-layout.md)
- [Auth Flow](../copilot/auth-flow.md)
- [Browser Terminal](../copilot/browser-terminal.md)
- [Resource Plane](../copilot/resource-plane.md)
- [Monitoring UI](../copilot/monitoring-ui.md)
- [Glass UI](../copilot/glass-ui.md)
- [Version Management](../copilot/version-management.md)
- [Security Audit Follow-up](../copilot/security-audit-followup.md)

## Conventions (Quick Reference)

- **English everywhere in source** — Korean is allowed only in human conversation.
- **Conventional Commits** (`feat:`, `fix:`, `chore:`, `docs:`, …).
- **Per-feature change notes** in `docs/features_change/YYYY-MM/YYYY-MM-DD-<name>.md` before each behaviour-changing commit.
- See [.github/copilot-instructions.md](https://github.com/dotnetpower/elb-dashboard/blob/main/.github/copilot-instructions.md) for the full charter.
