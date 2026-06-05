---
title: quick-deploy.sh backfills missing workspace discovery tags
description: quick-deploy.sh now adds the elb-* resource-group tags the SPA auto-discovery needs when they are absent, so a fast-deployed environment no longer strands signed-in users on the Setup Wizard.
tags:
  - infra
  - operate
---

# quick-deploy.sh backfills missing workspace discovery tags

## Motivation

The dashboard's first-run auto-discovery
([web/src/pages/Dashboard/configFromTags.ts](../../../web/src/pages/Dashboard/configFromTags.ts))
only treats a resource group as a BLAST workspace when it carries at least
one `elb-*` tag, and reads `elb-storage` / `elb-acr` / `elb-region` from those
tags. Those tags are applied by the full `azd up` path via
[scripts/dev/postprovision.sh](../../../scripts/dev/postprovision.sh)
`tag_workspace_resource_group`. A fast [scripts/dev/quick-deploy.sh](../../../scripts/dev/quick-deploy.sh)
cycle never runs provisioning, so a resource group that was only ever touched
by `quick-deploy.sh` (or had its tags stripped) leaves every signed-in user —
including users who genuinely hold read access — stuck on the Setup Wizard
instead of the dashboard.

## User-Facing Change

- `quick-deploy.sh` now backfills the `elb-*` workspace discovery tags on the
  deployment resource group when they are missing, in both the single-sidecar
  and `all` flows.
- "Add if missing" semantics: each desired tag is written only when absent or
  empty on the RG, so a pre-existing correct value is never clobbered by a
  stale shell variable. Tags whose value cannot be resolved from the
  environment are skipped rather than written empty.
- Best-effort: a caller without tag-write permission gets a warn line, not a
  failed deploy. Skip the step entirely with `ELB_SKIP_WORKSPACE_TAGS=1`.

## API/IaC Diff Summary

- `scripts/dev/quick-deploy.sh`:
  - New `ensure_workspace_tags` function (mirrors postprovision.sh's tag set:
    `elb-workload-rg`, `elb-acr-rg`, `elb-acr`, `elb-storage`, `elb-region`).
  - Called after `preflight_permission_check` / `ensure_provider_registration_once`
    in both deploy flows.
  - Uses per-key `az group show --query 'tags."<key>"'` to detect absence and
    `az tag update --operation Merge` to add only the missing keys — no new
    `jq` dependency.
- No application code, API, or Bicep changes.

## Validation Evidence

- `bash -n scripts/dev/quick-deploy.sh` — syntax OK.
- Logic walk-through: `assert_az_subscription_aligned` (called before the new
  step in both flows) exports `AZURE_RESOURCE_GROUP`, `ACR_NAME`,
  `STORAGE_ACCOUNT_NAME`, and `AZURE_LOCATION` from authoritative ARM lookups,
  so the desired tag values are populated; `AZURE_RESOURCE_GROUP` and
  `ACR_NAME` are already validated non-empty, guaranteeing at least
  `elb-workload-rg` / `elb-acr-rg` / `elb-acr` are written, which is enough to
  satisfy the SPA's `hasElb` discovery gate.
