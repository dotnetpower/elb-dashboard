---
title: Recover self-upgrade ACR build access
description: In-app self-upgrade now leases temporary ACR build access, restores the private network posture before deployment, and presents accurate busy and out-of-band state.
tags:
  - release
  - security
  - operate
---

# Recover self-upgrade ACR build access

## Motivation

A live commit-channel self-upgrade reached `az acr build` but the Microsoft-managed build agent could not authenticate to the private [Azure Container Registry](https://learn.microsoft.com/azure/container-registry/container-registry-intro). The standard GitHub Actions and CLI build paths already leased temporary build access; the in-app pipeline did not. The upgrade stopped in `failed_pre` before any Container App image change.

The same live review found two presentation gaps: refresh and start controls did not show busy motion, and an out-of-band deployment left the persisted image/rollback snapshot looking current even though it described an older self-upgrade.

## User-Facing Change

In-app self-upgrade now opens one bounded ACR build-access lease before the first sidecar build and restores the private network posture after the final build or any build failure. A failed open or restore is terminal: the pipeline remains `failed_pre` and never patches Container App images. The scheduled private-network reconciler remains the fail-safe if a worker disappears while access is open.

The Upgrade page now:

- animates Refresh, Check remote, Start upgrade, and build-log refresh busy states;
- replaces target controls with a dedicated active-upgrade summary and determinate progress bar while an upgrade is running;
- explains ACR build-access failures and confirms that no image update was applied; and
- clears stale current/rollback snapshots when remote discovery detects a terminal row from an out-of-band deployment.

## API / IaC Diff Summary

- Reused `api.services.acr_build_access.open_build_access` and `restore_build_access`; no new network-policy implementation was introduced.
- Preserved existing upgrade state names and HTTP schemas.
- Added fail-closed tests for lease open failure, restore failure, and successful restore-before-PATCH ordering.
- No RBAC assignment, Bicep resource, Storage network rule, or browser credential flow changed.

## Validation Evidence

- Live reproduction: ACR build run `decf` failed at registry login while the registry firewall was private; no frontend or terminal build started and no Container App PATCH occurred.
- Post-failure posture: ACR `publicNetworkAccess=Disabled`, `defaultAction=Deny`, `bypass=AzureServices`, active builds `0`.
- Focused ACR lease tests passed: 3.
- Upgrade route stale-snapshot test passed.
- Upgrade/ACR regression suite passed: 141 tests.
- Full backend suite passed: 6,021 tests with 4 fixture-dependent skips.
- Full frontend suite passed: 1,058 tests across 121 files.
- Ruff, ESLint, mypy debt ratchet, and frontend production build passed.
- The 242-operation OpenAPI contract and generated TypeScript API types were unchanged.
- Strict MkDocs build passed.
- Local API smoke passed: 27 of 27 checks.
- Blue/green flag-on and flag-off pipeline tests passed.
