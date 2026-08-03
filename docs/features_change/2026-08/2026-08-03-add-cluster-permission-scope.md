---
title: Add cluster permission scope correction
description: Evaluate the Add Cluster caller permission at the target workload resource group instead of subscription scope.
tags: [ui, auth, security]
---

# Add cluster permission scope correction

## Motivation

The **Add Cluster** action checked the signed-in caller's write capability at
subscription scope. The standard least-privilege deployment grants a researcher
Contributor on the workload resource group (`rg-elb-cluster`), not on the whole
subscription. The dashboard therefore disabled the button with a permission
warning even though the caller could create an AKS resource in the selected
resource group.

The live permission endpoint confirmed the mismatch:

- subscription scope: `can_write=false`, `reason=no_role_at_scope`
- `rg-elb-cluster` scope: `Contributor`, `can_write=true`

## User-facing change

The button now evaluates caller permissions at the configured workload resource
group. A Contributor on that resource group can open the provisioning dialog;
a Reader remains denied with the existing role tooltip.

The dashboard managed identity still performs the actual Azure writes. The
provisioning preflight independently verifies its resource-group Contributor,
runtime RBAC, and narrow subscription-level permission for the AKS-created
`MC_*` node resource group. This change does not broaden Azure roles or bypass
that preflight.

## Validation

- Read-only deployed permission comparison at subscription and workload-RG
  scopes.
- `npm --prefix web run test -- --run`
- `npm --prefix web run lint`
- `npm --prefix web run build`
- `DISABLE_MKDOCS_2_WARNING=true uv run mkdocs build --strict`