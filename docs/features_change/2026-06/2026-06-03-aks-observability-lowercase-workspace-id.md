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

Root cause: Azure returns the App Insights component's `WorkspaceResourceId`
**fully lowercased** — `/subscriptions/…/resourcegroups/defaultresourcegroup-se/
providers/microsoft.operationalinsights/workspaces/defaultworkspace-…-se`. The
enable route's `_RE_WORKSPACE_ID` validator required the canonical ARM casing
(`/resourceGroups/`, `Microsoft.OperationalInsights`) and was **case-sensitive**,
so the lowercased id never matched. ARM resource IDs are case-insensitive on
their literal path segments, so the strict matcher was wrong.

The `<base64-redacted>` in the error was a red herring: the `sanitise()` helper
masked a ≥40-char run of the (valid) id (`9d60a7301a80/resourcegroups/…`) as a
suspected base64 blob when shaping the 400 message — display-only, not the cause.

## User-facing change

Enabling AKS Observability now succeeds with the default Log Analytics
workspace id that Azure auto-creates and returns lowercased. No UI change.

## API / IaC diff summary

- `api/routes/settings/aks_observability.py`: compile `_RE_WORKSPACE_ID` with
  `re.IGNORECASE` and document why (ARM literal segments are case-insensitive;
  App Insights returns the id lowercased). Behaviour is otherwise unchanged —
  the subscription GUID / RG / workspace-name char classes already covered both
  cases.

## Validation evidence

- `uv run pytest -q api/tests/test_settings_aks_observability.py` → 8 passed,
  including the new `test_enable_accepts_lowercased_arm_workspace_id` regression
  (full lowercased `resourcegroups` / `microsoft.operationalinsights` id) and
  the still-green `test_enable_rejects_invalid_workspace_resource_id`.
- `uv run ruff check` on both touched files → clean.
