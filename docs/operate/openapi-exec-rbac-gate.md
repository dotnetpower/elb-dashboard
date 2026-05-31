---
title: OpenAPI execution RBAC gate
description: Opt-in enforcement that restricts who can drive state-changing OpenAPI "Try it" / curl calls through the admin-token proxy, gating mutating verbs on the caller's [Azure RBAC](https://learn.microsoft.com/azure/role-based-access-control/overview) write role on the target resource group.
tags:
  - operate
  - security
  - blast
---

# OpenAPI execution RBAC gate

## Why this exists

The dashboard's OpenAPI menu (`/docs`) and any `curl` against
`/api/aks/openapi/proxy` are served by a proxy that auto-injects the admin
`X-ELB-API-Token` and forwards the call to the `elb-openapi` pod. The proxy is
protected by `require_caller`, which validates **tenant membership only** — it
does not perform a per-caller [Azure RBAC](https://learn.microsoft.com/azure/role-based-access-control/overview)
check. Because On-Behalf-Of token flows are forbidden by charter §12 (all
Azure work runs under the shared managed identity), the historical behaviour is
that **any authenticated tenant member — even a subscription Reader — can drive
state-changing OpenAPI calls** (for example `POST /v1/jobs` to submit a BLAST
job) through the admin token.

Two mitigations ship for this:

1. **Forensic audit trail (always on).** Every state-changing proxy call
   (`POST`/`PUT`/`PATCH`/`DELETE`) writes a token-free audit row recording the
   caller OID, tenant, method, and target path. See
   `api/services/openapi/proxy_audit.py`.
2. **Opt-in RBAC gate (this document).** When enabled, the proxy only forwards
   a mutating call if the caller actually holds a write role on the target
   resource group.

## What the gate does

`api/services/openapi/exec_gate.py` (`evaluate_openapi_exec_gate`) decides, in
order:

| Step | Condition | Outcome |
|------|-----------|---------|
| 1 | `ENFORCE_OPENAPI_EXEC_RBAC` off (default) | **allow** — legacy behaviour |
| 2 | Read-only method (`GET`/`HEAD`/`OPTIONS`) | **allow** — never gated |
| 3 | Dev-bypass identity (local debug) | **allow** |
| 4 | RBAC lookup indeterminate (`degraded`) | **deny 403** `openapi_exec_rbac_indeterminate` (fail-closed) |
| 5 | Caller holds a write role at the RG scope | **allow** |
| 6 | Otherwise | **deny 403** `openapi_exec_forbidden` |

"Write role" reuses the tested `compute_caller_permissions` definition:
Owner, Contributor, Azure Kubernetes Service Contributor, AKS RBAC Admin /
Cluster Admin / Writer.

The RBAC lookup is cached 60 s per `(caller OID, scope)`, so the gate adds at
most one ARM `roleAssignments.list_for_subscription` call per caller per scope
per minute.

### Fail-closed, not fail-open

The dashboard's permission helper (`compute_caller_permissions`) degrades
**open** on enumeration failure — that is a deliberate UX affordance so a
transient ARM hiccup never greys out a button. The execution gate **inverts**
this: when enforcement is on and the lookup is indeterminate, it **denies**.
Otherwise a managed identity that lacks
`Microsoft.Authorization/roleAssignments/read` would silently disable the gate.

## How to enable it

The gate ships **default-OFF** per charter §12a Rule 4. Set the container app
env var to a truthy value (`1` / `true` / `yes` / `on`):

```bicep
// infra/modules/containerAppControl.bicep
{ name: 'ENFORCE_OPENAPI_EXEC_RBAC', value: 'true' }
```

Re-run `scripts/dev/postprovision.sh` (or `azd provision`) to roll the new
revision.

### Prerequisite — MI role-read permission

When enabled, the shared api managed identity must be able to read the
caller's role assignments. Grant the
[Reader](https://learn.microsoft.com/azure/role-based-access-control/built-in-roles#reader)
built-in (or any role that includes
`Microsoft.Authorization/roleAssignments/read`) to `id-elb-dashboard-*` at the
subscription scope. Without it, every state-changing call fails closed with
`openapi_exec_rbac_indeterminate`.

## Planned default flip

This guard stays **default-OFF** until at least one full release cycle of
dogfood with the gate forced ON and a green
[`api/tests/test_persona_matrix.py`](https://github.com/dotnetpower/elb-dashboard/blob/main/api/tests/test_persona_matrix.py)
run. Earliest flip target: **2026-07** (revisit after the audit-trail data
confirms which personas drive `/v1/` mutations in practice). The flip is its
own PR that changes the bicep default and removes the OFF-path note here.

## Validation

```bash
uv run pytest -q api/tests/test_openapi_exec_gate.py
```
