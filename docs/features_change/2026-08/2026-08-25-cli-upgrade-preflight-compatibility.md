---
title: CLI upgrade preflight compatibility
description: Recognize current Storage private endpoint responses and allow resource-group-scoped rolling upgrade operators without weakening deployment RBAC gates.
tags: [operate, security]
---

# CLI upgrade preflight compatibility

## Motivation

The rolling update dry-run blocked a healthy deployment for two independent reasons. The current [Azure CLI](https://learn.microsoft.com/cli/azure/) response exposes each Storage private endpoint connection state at the flattened object level, while the preflight queried only the legacy nested `properties` shape and reported zero approved connections. The signed-in operator also held `Contributor` on the platform resource group, which covers the ACR build and Container App patch, but the caller preflight accepted only subscription-scoped assignments.

## User-facing change

- Storage isolation parity now counts approved [private endpoints](https://learn.microsoft.com/azure/private-link/private-endpoint-overview) from both the current flattened Azure CLI response and the legacy nested response.
- Rolling upgrade read/write checks accept `Reader` or `Contributor` respectively when granted on the platform resource group.
- Full deployment, RBAC doctor, and RBAC auto-fix modes keep their existing subscription-scoped [Azure RBAC](https://learn.microsoft.com/azure/role-based-access-control/overview) requirements.
- Failure guidance for rolling updates recommends the platform resource-group scope instead of an unnecessarily broad subscription grant.

## API and infrastructure summary

- No API, image, Bicep, role assignment, or deployed resource contract changed.
- The change is limited to `scripts/dev/cli-upgrade.sh`, `scripts/dev/_caller-precheck.sh`, and their local regression tests.

## Validation

- Live `cli-upgrade.sh api --dry-run` before the fix reproduced `approvedPrivateEndpoints=0` despite three approved Storage private endpoints.
- The same dry-run after the fix reported `approvedPrivateEndpoints=3` and accepted the operator's platform-RG `Contributor` assignment.
- `uv run pytest -q api/tests/test_cli_upgrade_preflight.py` - `8 passed`.
- Storage remained `publicNetworkAccess=Disabled`, `defaultAction=Deny`; `/api/health/ready` continued to report `azure_storage.status=ok` over the private endpoint path.