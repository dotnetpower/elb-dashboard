---
title: Local Storage auto-open default-off hardening
description: Prevent ordinary local API and VS Code debug launches from implicitly opening the deployed workload Storage public network surface.
tags:
  - security
  - contributor
---

# Local Storage auto-open default-off hardening

## Motivation

During post-deployment validation, the live [Azure Storage](https://learn.microsoft.com/azure/storage/common/storage-introduction) account was observed in `publicNetworkAccess=Enabled` and `defaultAction=Allow`. Azure Activity Log and local API logs correlated the write with a local `/api/blast/databases` request: `local-run.sh` had silently defaulted `LOCAL_DEBUG_AUTO_OPEN_STORAGE` to `true` even though the service gate and operator documentation described it as default-OFF.

The account was immediately restored to `publicNetworkAccess=Disabled` and `defaultAction=Deny`; all three private endpoints remained approved and `/api/health/ready` continued to report Storage healthy.

## User-facing change

Ordinary `scripts/dev/local-run.sh start|api` and VS Code API debug launches no longer opt into Storage auto-open. Developers must explicitly set `LOCAL_DEBUG_AUTO_OPEN_STORAGE=true` or use the existing `storage-on` / `auth-on` workflows. Every explicit open is restricted to one detected caller IPv4 with `defaultAction=Deny` and `bypass=None`; caller-IP discovery failure leaves the account closed. Local authentication behavior, Azure RBAC, browser streaming, and the deployed `CONTAINER_APP_NAME` refusal guard are unchanged.

## API and infrastructure diff

- Changed the local launcher fallback for `LOCAL_DEBUG_AUTO_OPEN_STORAGE` from `true` to `false`.
- Removed the VS Code launch profile's hard-coded `true`, allowing the service's default-OFF contract or an explicit debug environment override to apply.
- Replaced the stale `defaultAction=Allow` / `bypass=AzureServices` workaround in both local-debug helpers with one caller IPv4 rule under `defaultAction=Deny` / `bypass=None`.
- Ordered the shell transition as close-first, rule replacement, then enable-last; its close path disables access before removing rules and verifies the final production posture.
- Added a fail-closed same-region Azure topology check. Azure Storage IP rules do not apply to same-region Azure clients, so the API, shell helper, and Storage card now direct those developers to the deployed private-endpoint path without performing an ARM write.
- Added a source-contract regression test for both local launch entry points.
- No API schema, Service Bus wire contract, Azure role assignment, Bicep resource, or deployed Container App setting changed.

## Validation

- `uv run pytest -q api/tests/test_storage_public_access.py`
- `bash -n scripts/dev/storage-public-access.sh scripts/dev/local-run.sh`
- `uv run pytest -q api/tests/test_persona_matrix.py`
- `uv run ruff check api/tests/test_storage_public_access.py`
- Live ARM state: `publicNetworkAccess=Disabled`, `defaultAction=Deny`, zero IP rules, three approved private endpoints.
- Live readiness: `/api/health/ready` returned HTTP 200 with `azure_storage.status=ok` after closure.
- Live topology probe: the developer host and Storage account were both in Korea Central; a selected-network request returned the documented 403, and cleanup restored `Disabled/Deny/None` with zero IP rules.
