# OpenAPI internal LoadBalancer subnet-RBAC recovery route (issue #33)

## Motivation

When an AKS cluster is created **out-of-band** (manual `az aks create`, or
delete + recreate outside the dashboard's `provision_aks` task), the
`elb-openapi` internal LoadBalancer Service stays `EXTERNAL-IP <pending>` and
every `/api/aks/openapi/{spec,proxy}` call (and the Service Bus drain path)
degrades with `openapi_endpoint_unreachable`. Root cause: the cluster
control-plane identity is missing **Network Contributor** on the BYO `snet-aks`
subnet, so the Azure cloud-provider cannot allocate the LB frontend IP. The
provision task grants this automatically; a manual recreate skips it.

## User-facing change

- New recovery action: an operator can re-run the exact grant the provision
  task performs, idempotently, without hand-crafting an `az role assignment`
  command. The response carries a token-cache caveat note so the propagation
  delay is not misread as a failure.

## API / IaC diff summary

- **Root-cause fix** — `deploy_openapi_service` (`api/tasks/openapi/deploy.py`)
  now calls `ensure_openapi_lb_subnet_rbac` **before** `kubectl apply` creates
  the `elb-openapi` Service, reusing the cluster it already fetched. The grant
  therefore happens on every deploy (dashboard or post-recreate), so the gap can
  no longer be introduced by a cluster created out-of-band. Granting before the
  Service is created also sidesteps the cloud-controller token-cache lag that
  makes a post-hoc grant take effect only after a stop/start. Best-effort: a
  grant failure logs a warning and does not fail the deploy (mirrors
  `provision_aks`); the result payload carries an additive
  `openapi_deploy.lb_subnet_rbac` status for diagnosis.
- **New route**: `POST /api/aks/openapi/lb-subnet-rbac` (`require_caller`,
  synchronous, mirrors the `/api/aks/peer-with-platform` recovery pattern) — the
  manual fallback for a cluster whose `elb-openapi` was applied before this
  integration shipped. Body: `{subscription_id?, resource_group, cluster_name}`.
  Returns `{status: granted, principal_id, subnet_id, role, note}` or
  `{status: skipped, reason: managed_vnet_mode | cluster_identity_unresolved}`;
  `502 lb_subnet_rbac_grant_failed` when the grant raises.
- **New service helper**: `api/services/aks/openapi_lb_rbac.py`
  `ensure_openapi_lb_subnet_rbac(...)` — resolves the cluster control-plane
  identity (SystemAssigned or first UserAssigned) and its node subnet
  (`first_node_subnet_id`, reused from `node_subnet_nsg.py`), then delegates to
  the idempotent `grant_network_contributor_on_subnet`. Managed-VNet clusters
  are a graceful skip. Accepts an optional pre-fetched `cluster` so the deploy
  task avoids a duplicate ARM `managed_clusters.get`.
- No IaC change. No role narrowed — the grant is **additive** (charter §12a
  Rule 1 N/A), and ships no new env gate.
- **Docs**: new Troubleshooting section "OpenAPI 'Try it' / Service Bus drain
  unreachable after a manual cluster recreate" (redeploy is the primary fix; the
  recovery route is the no-redeploy fallback).

## Persona impact (§12a)

- In scope: rbac (additive grant), auth (new `require_caller` route).
- RBAC change is single-PR safe — no role narrowed, no `roleAssignments`
  resource removed from Bicep (this is a runtime grant, not a Bicep diff). No
  phase-1/phase-2 split needed.
- No `Depends(require_caller)` added to any SSE stream.
- No new `STRICT_*`/`ENFORCE_*` gate required (deploy-time best-effort grant +
  manual recovery action).

## Scope / follow-up (kept open in #33)

This change ships the **root-cause fix** (deploy-time grant) plus the manual
recovery route + docs. The remaining issue #33 items — automatic
`lb_subnet_rbac_missing` detection on the spec/proxy degraded payload and a
one-click SPA button — are deferred and tracked in the issue.

## Validation evidence

- `uv run pytest -q api/tests/test_openapi_lb_subnet_rbac.py` — 11 passed
  (helper: BYO grant, UserAssigned resolve, managed-VNet skip, identity-unresolved
  skip, idempotent repeat, passed-cluster-no-ARM-get; route: 400 / delegates /
  502; deploy integration: grant-before-apply reusing the cluster, and
  best-effort tolerance of a grant failure).
- `uv run pytest -q api/tests/test_route_contracts.py api/tests/test_openapi_task.py
  api/tests/test_tasks_facade_contract.py` — green.
- `uv run ruff check api` — clean.
- `uv run python scripts/docs/check_frontmatter.py` + `mkdocs build --strict` — OK.

