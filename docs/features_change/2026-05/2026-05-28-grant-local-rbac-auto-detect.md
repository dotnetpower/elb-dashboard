---
title: grant-local-rbac.sh — auto-detect storage / ACR from azd env
description: Standalone grant-local-rbac.sh now mirrors local-debug-auth.sh and auto-resolves workload Storage / ACR names from azd env values, with a stelbdashboard* / acrelbdashboard* single-match fallback.
tags:
  - contributor
  - setup
---

## Motivation

Running `scripts/dev/grant-local-rbac.sh` directly on a fresh `azd up`
deployment failed with:

```
ERROR: storage account 'elbstg01' not found in 'rg-elb-01'
```

The script's hard-coded defaults match the docs/auth.md example
(`elbstg01` / `rg-elb-01` / `elbacr01` / `rg-elbacr-01`), not the
auto-suffixed names produced by `azd up`
(`stelbdashboard…` / `acrelbdashboard…` / `rg-elb-dashboard`). The
wrapper `local-debug-auth.sh` already auto-detected from
`azd env get-values`, so users hitting the standalone script had a
worse experience than users going through the wrapper.

## User-facing change

`scripts/dev/grant-local-rbac.sh` (no flag changes) now resolves the
workload names in this order:

1. Explicit `--storage` / `--storage-rg` / `--acr` / `--acr-rg` flags
   (unchanged).
2. `azd env get-values` keys `STORAGE_ACCOUNT_NAME`,
   `AZURE_RESOURCE_GROUP`, `ACR_NAME` when those flags are not set.
3. Docs example defaults (`elbstg01` / `rg-elb-01` / `elbacr01` /
   `rg-elbacr-01`) as a final fallback.
4. If the chosen storage / ACR does not exist and exactly one
   `stelbdashboard*` / `acrelbdashboard*` resource is reachable in the
   subscription, use that and print an `auto-resolved` notice.

This matches the resolution logic already present in
`scripts/dev/local-debug-auth.sh` so both entry points behave the same.

## API / IaC diff summary

- `scripts/dev/grant-local-rbac.sh`
  - Initial `STORAGE` / `STORAGE_RG` / `ACR` / `ACR_RG` are now empty;
    docs example values become `*_DEFAULT` fallbacks.
  - Added an `azd env get-values` block (uses `azd` only if installed)
    that fills any unset name before the docs defaults apply.
  - Added a `stelbdashboard*` single-match fallback and a symmetric
    `acrelbdashboard*` fallback for ACR.

No flag, no help text, no behavioural change for callers that already
pass explicit flags (including `local-debug-auth.sh`).

## Validation evidence

- `bash -n scripts/dev/grant-local-rbac.sh` — clean.
- `scripts/dev/grant-local-rbac.sh --dry-run` against the active
  `rg-elb-dashboard` deployment:

  ```
  auto-resolved storage: stelbdashboard3abp67bppe (rg: rg-elb-dashboard)
  auto-resolved acr: acrelbdashboard3abp67bppe (rg: rg-elb-dashboard)
  …
  Summary: created=0 skipped=5 failed=0
  ```

  All five role assignments (Storage Blob / Table Data Contributor,
  Storage Account Contributor, Reader on RG, AcrPull on ACR) were
  recognised as already present.
