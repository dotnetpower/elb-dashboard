---
title: Accept lowercased ARM workspace id when enabling AKS Container Insights
description: Fix the "invalid workspace_resource_id" error raised when enabling AKS Observability with the lowercased Log Analytics workspace id Azure returns.
tags:
  - operate
  - infra
---

# Accept lowercased ARM workspace id when enabling AKS Container Insights

## Motivation

Enabling **AKS Observability** (Container Insights) from the Settings panel
failed with:

```
invalid workspace_resource_id: '/subscriptions/b052302c-4c8d-49a4-aa2f-<base64-redacted>'
```

The UI showed the resolved workspace as READY
(`defaultworkspace-b052302c-…-se`) yet the enable request was rejected at the
HTTP boundary.

Two independent defects combined:

1. **Backend (case-sensitive validator).** The enable route's
   `_RE_WORKSPACE_ID` validator required the canonical ARM casing
   (`/resourceGroups/`, `Microsoft.OperationalInsights`) and was
   **case-sensitive**. ARM resource IDs are case-insensitive on their literal
   path segments, and an Azure-auto-created default Log Analytics workspace id
   arrives lowercased (`/subscriptions/…/resourcegroups/defaultresourcegroup-se/
   providers/microsoft.operationalinsights/workspaces/defaultworkspace-…-se`),
   so the strict matcher rejected a perfectly valid id with HTTP 400.
2. **Frontend (stale cached workspace).** The Settings panel stores the last
   resolved workspace id in `localStorage["elb-prefs"].appInsightsWorkspaceResourceId`
   and the `enable` handler sent that stored value **without re-resolving**. A
   stale cached id (e.g. a default workspace captured before the App Insights
   component was re-pointed to `log-elb-dashboard`) was therefore posted even
   though the displayed App Insights component now backs a different workspace.

Verified against the live subscription: `appi-elb-dashboard` exists once (in
`rg-elb-dashboard`) and its real backing workspace is `log-elb-dashboard`, yet
the UI was sending the lowercased `defaultworkspace-…-se` id — proving the value
came from a stale pref, not the current component.

The `<base64-redacted>` in the error was a red herring: the `sanitise()` helper
masked a ≥40-char run of the (valid) id (`9d60a7301a80/resourcegroups/…`) as a
suspected base64 blob when shaping the 400 message — display-only, not the cause.

## User-facing change

Enabling AKS Observability now succeeds with any valid ARM workspace id
regardless of casing, and the enable action always patches the cluster with the
workspace that currently backs the named App Insights resource — a stale cached
workspace id can no longer be sent.

## API / IaC diff summary

- `api/routes/settings/aks_observability.py`: compile `_RE_WORKSPACE_ID` with
  `re.IGNORECASE` and document why (ARM literal segments are case-insensitive;
  Azure returns auto-created workspace ids lowercased). Behaviour is otherwise
  unchanged — the subscription GUID / RG / workspace-name char classes already
  covered both cases.
- `web/src/components/SettingsPanel.tsx`: the `enable` handler now always calls
  `resolveWorkspace()` to fetch the current backing workspace from the named
  App Insights component instead of trusting the stored
  `appInsightsWorkspaceResourceId` pref. The workspace is always derived from
  the component (there is no manual workspace-id field), so re-resolving is
  authoritative and eliminates the stale-cache class entirely.

## Validation evidence

- `uv run pytest -q api/tests/test_settings_aks_observability.py` → 8 passed,
  including the new `test_enable_accepts_lowercased_arm_workspace_id` regression
  (full lowercased `resourcegroups` / `microsoft.operationalinsights` id) and
  the still-green `test_enable_rejects_invalid_workspace_resource_id`.
- `uv run ruff check` on both touched files → clean.
- `npx tsc --noEmit` and `npx eslint src/components/SettingsPanel.tsx` → clean
  (the single remaining eslint warning is a pre-existing one at line 1949,
  unrelated to this change).
- Backend fix confirmed live in Container App revision `ca-elb-dashboard--0000090`
  (created after the edit); the frontend fix reaches users on the next frontend
  deploy + browser refresh.
