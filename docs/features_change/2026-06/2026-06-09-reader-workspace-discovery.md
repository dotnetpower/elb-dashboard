---
title: Auto-discover the workspace for reader-only callers
description: Consult the managed-identity ARM proxy when the caller's direct resource-group list omits the elb workspace, so a reader lands on the dashboard instead of the SetupWizard.
tags:
  - auth
  - ui
---

# Workspace discovery — reader-only callers see the dashboard, not the wizard

## Motivation

A caller with only resource-group-scoped `Reader` (on an *unrelated* RG) opened
a fresh browser and was dropped into the SetupWizard even though a valid
ElasticBLAST deployment existed in the subscription. Root cause: auto-discovery
listed resource groups via direct ARM (the user's token) and only fell back to
the managed-identity proxy when that list was **empty or threw**. A reader whose
direct list was *non-empty but incomplete* (it returned their one unrelated RG,
not the `elb-*`-tagged workload RG) never triggered the fallback, so discovery
found no workspace and showed the wizard.

## User-facing change

When the direct ARM resource-group scan for a subscription finds **no**
`elb-*`-tagged workspace, discovery now also consults the backend managed-identity
proxy (which sees the whole subscription via the shared identity's `Reader`
role) and merges the results. A reader who has a valid deployment in the
subscription now auto-lands on the dashboard with the correct workspace
selected, instead of the SetupWizard.

## Code summary

- [web/src/pages/Dashboard/discoveryRgs.ts](../../../web/src/pages/Dashboard/discoveryRgs.ts) —
  new pure helpers `hasElbWorkspace` + `mergeRgsByName`.
- [web/src/pages/Dashboard/useWorkspaceDiscovery.ts](../../../web/src/pages/Dashboard/useWorkspaceDiscovery.ts) —
  per-subscription: direct ARM first; when no elb workspace is found, fetch the
  MI proxy list and merge so the workload RG surfaces regardless of per-user RBAC.

## Validation

- `npx vitest run src/pages/Dashboard/discoveryRgs.test.ts` — 5 tests pass,
  including the "incomplete direct list" regression.
- `cd web && npm run build` clean.
