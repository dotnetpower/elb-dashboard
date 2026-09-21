---
title: Detect stale AKS bootstrap roles before provisioning
description: AKS preflight now verifies the live custom-role actions instead of trusting a matching role name, preventing delayed Contributor failures on legacy deployments.
tags:
  - security
  - infra
  - ui
---

# Detect stale AKS bootstrap roles before provisioning

## Motivation

The AKS create preflight treated any subscription assignment named
`Elb Workload RG Creator` as capable of bootstrapping Contributor on a new
cluster resource group. Deployments created before the role was extended could
still have that display name while lacking
`Microsoft.Authorization/roleAssignments/read` and `write`.

The stale role could create the resource group, so validation appeared healthy,
but the worker then failed to self-grant Contributor before the AKS ARM request.
The user only saw the [Azure RBAC](https://learn.microsoft.com/azure/role-based-access-control/overview)
failure after provisioning had started.

## User-Facing Change

- AKS preflight now reads the assigned custom role's live Actions.
- It also verifies `conditionVersion=2.0` and the constrained delegation
  condition for Contributor, User Access Administrator, role-assignment writes,
  and `ServicePrincipal` targets.
- Resource-group creation and Contributor self-bootstrap are evaluated as
  separate capabilities.
- A legacy role still satisfies the resource-group creation requirement, but it
  no longer produces a false successful preflight when Contributor is absent.
- Host-mode API, worker, and VS Code debug sessions now import the deployed
  dashboard principal ID from the selected azd environment. Local requests no
  longer degrade this check to a permissive `warn` merely because `.env` omits
  that metadata.
- The failed check explains that infrastructure provisioning must update the
  custom role, or that Contributor can be granted directly at resource-group
  scope.
- The post-failure card no longer claims the identity has only Reader; it names
  legacy `Elb Workload RG Creator` drift as a possible cause.

## API / IaC Diff Summary

- `api.services.rbac_preflight.aks_create_rbac_check` keeps the existing
  `PreflightCheck` and `details.missing[]` response contracts.
- Failure details add `custom_role_missing_actions` and preserve
  `cluster_rg_bootstrap_capable`; `custom_role_assignment_issues` reports stale
  or missing constrained-delegation clauses.
- `scripts/dev/probe_capabilities.py` registers the same structural contract as
  a required postprovision probe. A deployment with a stale role definition or
  assignment condition now exits non-zero instead of reporting success.
- Postprovision runs that probe in `--structural-only` mode, then invokes
  `check-mi-rbac.sh --strict` for the full expected role manifest and finally
  `probe-deployed-capabilities.sh`. The latter authenticates to the deployed
  API and checks readiness, subscription/RG discovery, Storage, and ACR; those
  Azure calls therefore run under the actual sidecar UAMI inside the private
  VNet instead of the deployer's local identity.
- `scripts/dev/local-run.sh` imports only `SHARED_IDENTITY_PRINCIPAL_ID`; it does
  not import the managed identity client ID, so local authentication continues
  to use the developer's Azure CLI credential. The azd lookup is non-interactive
  and bounded to eight seconds so a missing environment cannot stall startup.
- No route shape or task state changed. The checked-in Bicep role resources
  already contained the required Actions and condition; the live legacy role
  definition and its existing assignment were updated in place from that
  module without provisioning another Azure resource.

## Validation Evidence

- RBAC-focused tests -> 23 passed across preflight, postprovision probe, and
  local identity-environment contracts.
- `npm --prefix web test -- --run src/components/cards/ClusterCard/armErrorClassifier.test.ts`
  -> 6 passed.
- `uv run pytest -q api/tests/test_local_run_identity_env.py -m ""` -> 4 passed.
- Full backend suite including slow/subprocess tests -> 6,129 passed with 4
  fixture-dependent skips.
- Full frontend suite -> 1,048 passed across 120 files.
- Ruff, the mypy debt ratchet, ESLint, the 242-operation OpenAPI contract,
  generated TypeScript contract check, and frontend production build passed.
- Live read-only evaluation against the affected Azure environment returned
  `status=fail` and identified both missing role-assignment Actions before task
  submission.
- A real `POST /api/aks/preflight` against the restarted host-mode API returned
  `ok=false` with the same stale-role diagnosis.
- The targeted subscription-scope role-module what-if reported exactly two
  in-place `Modify` operations: the existing custom role definition and its
  existing assignment. No create/delete or application-resource change was
  present.
- Subscription deployment `elb-rbac-bootstrap-fix-20260920` completed with
  `Succeeded`. The post-deploy structural probe reported
  `Elb Workload RG Creator definition + assignment OK`, and the live preflight
  changed from `ok=false` to `ok=true` for `rg-elb-cluster`.
- The exact structural-only command completed with `ok=1, fail=0`; the deployed
  runtime probe passed readiness/Storage Table, Azure discovery, Storage
  management/container listing, and ACR management/repository listing.
- The full read-only UAMI role audit completed with `ok=10, fail=0` and one
  informational warning because no AKS cluster exists yet.
