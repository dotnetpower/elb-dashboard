# 2026-05-27 — `provision_aks` self-grants runtime RBAC on the cluster RG

## Motivation

The 2026-05-25 fix
([`docs/features_change/2026-05/2026-05-25-cli-upgrade-rbac-autogrant.md`](2026-05-25-cli-upgrade-rbac-autogrant.md))
closed the OpenAPI-deploy `workload identity setup failed; OpenAPI pod
would have no AZURE_CLIENT_ID.` gap through three operator-side
self-healers:

1. New Bicep module
   [`infra/modules/workloadClusterRoles.bicep`](../../../infra/modules/workloadClusterRoles.bicep)
   that grants the dashboard MI `Contributor` + `User Access Administrator`
   on `rg-elb-cluster` — but only when the operator sets
   `AKS_CLUSTER_RESOURCE_GROUP` and re-runs `azd provision`.
2. `cli-upgrade.sh` preflight that calls `grant-runtime-rbac.sh`.
3. `postprovision.sh` safety net that calls the same helper.

All three depend on an operator action AFTER the SPA wizard creates the
AKS cluster. A real env (sub `b052302c-…`,
ca-elb-dashboard / rg-elb-dashboard / rg-elb-cluster) hit the gap again
on 2026-05-27 because the operator created the cluster through the SPA
but never re-ran `azd provision`. The fix at that moment was to run
`grant-runtime-rbac.sh` by hand.

We want this gap to be **structurally impossible** on the next cluster
create, not "remediable in 5 minutes if you know which script to run".

## User-facing change

* `api.tasks.azure.provision.provision_aks` (Celery) now performs a
  self-grant of `Contributor` + `User Access Administrator` to the
  dashboard MI on the AKS cluster RG immediately after the
  `ensure_aks_runtime_rbac` (kubelet) step succeeds.
* The result is published into the final `PROGRESS{phase: completed}`
  payload AND the task return value under
  `dashboard_mi_rbac: {roles_assigned, roles_failed, mi_principal_id,
  cluster_resource_group, recovery_command}` so the SPA can render a
  badge / recovery affordance on the cluster card.
* The self-grant is **best-effort**: if the dashboard MI lacks
  `Microsoft.Authorization/roleAssignments/write` at the target scope
  (pre-Part-C deployments), the failure is recorded in
  `dashboard_mi_rbac.roles_failed` along with `recovery_command` (the
  exact `bash scripts/dev/grant-runtime-rbac.sh --yes …` invocation),
  but the provision task is **not failed**. The cluster is fully
  usable; only the later "Deploy elb-openapi" click would surface the
  remaining gap (with the same clear error string we already emit).

## Why "Part A" alone is not enough → Part C

`provision_aks` runs as the dashboard MI. Before this PR the MI's
sub-scope custom role
[`infra/modules/workloadRgCreatorRole.bicep`](../../../infra/modules/workloadRgCreatorRole.bicep)
("Elb Workload RG Creator") allowed only `resourceGroups/{read,write,
delete}` + `deployments/*`. It did **not** allow `roleAssignments/write`,
so the new self-grant from Part A would fail with `AuthorizationFailed`
on a fresh environment — turning Part A into a no-op silently logged in
the worker.

**Part C** extends the same custom role with:

* `Microsoft.Authorization/roleAssignments/read`
* `Microsoft.Authorization/roleAssignments/write`

and adds an ABAC condition on the role assignment of the custom role
(Constrained Role Assignment Delegation) that restricts the write to:

* `RoleDefinitionId` ∈ a 5-GUID whitelist:
  * Contributor `b24988ac-6180-42a0-ab88-20f7382dd24c`
  * User Access Administrator `18d7d88d-d35e-4fb5-a5c3-7773c20a72d9`
  * Storage Blob Data Contributor `ba92f5b4-2d11-453d-a403-e96b0029c9fe`
  * AcrPull `7f951dda-4ed3-4680-a7ca-43fe172d538d`
  * Azure Kubernetes Service Cluster User Role `4abbcc35-e782-43d8-92c5-2d3f1bd2253f`
* `PrincipalType` equals `ServicePrincipal` (so the MI can never
  grant a role to a User or Group).

All five are roles the dashboard already assigns through its normal
flow (`ensure_aks_runtime_rbac`, `setup_workload_identity`), so this
just removes the chicken-and-egg permission gap without widening what
the MI can actually do operationally. The MI still cannot:

* assign `Owner` (or any other role outside the whitelist) anywhere;
* assign anything to a User or Group;
* delete role assignments (no `roleAssignments/delete` added — the
  contract is "RBAC is never revoked by the dashboard");
* create / modify role definitions (no `roleDefinitions/*` added).

## API / IaC diff summary

* **Python**: new helper
  `api.tasks.azure.rbac.ensure_dashboard_mi_cluster_rg_roles` +
  `_facade._ensure_dashboard_mi_cluster_rg_roles` re-export. New phase
  string `ensuring_dashboard_mi_rbac` registered in `_RBAC_SUB_PHASES`.
  `provision_aks` calls the helper after the existing
  `_ensure_aks_runtime_rbac` block and embeds the result in the
  completion payload + return value.
* **Bicep**:
  [`infra/modules/workloadRgCreatorRole.bicep`](../../../infra/modules/workloadRgCreatorRole.bicep)
  adds `Microsoft.Authorization/roleAssignments/{read,write}` to the
  custom role and an ABAC condition on the role assignment. The role
  definition `name` (`guid(subscription().id, roleName)`) is unchanged
  so the existing definition is patched in place on next `azd provision`
  (idempotent — Bicep deploys the role definition as
  create-or-update).
* **ARM**: `infra/main.json` regenerated via `az bicep build`.
* **Tests**: 6 new tests in `api/tests/test_azure_tasks.py`
  + 1 new integration test in `api/tests/test_azure_provision_aks.py`
  (33 → 39 tests; all 39 in the touched files pass).

No SPA / docs / contract change for routes — the new
`dashboard_mi_rbac` payload key is additive on `/api/tasks/{id}`.

## Operator behavior matrix

| Deployment state | Self-grant outcome | Operator action needed |
|---|---|---|
| Fresh deployment after Part C ships → `azd provision` ran → MI has the extended custom role | `roles_assigned: [Contributor, User Access Administrator]` | None — works automatically on the first SPA-driven cluster create |
| Pre-Part-C deployment, never re-provisioned | `roles_failed: {…AuthorizationFailed…}` + `recovery_command` populated | Run `recovery_command` once (or re-run `azd provision` so Part C lands), then click "Deploy elb-openapi" again |
| Local dev with no `SHARED_IDENTITY_PRINCIPAL_ID` env var | `skipped: True` | None (irrelevant — no MI to grant to) |
| Cluster RG == deploy RG (rare; same MI already has Contributor+UAA from `controlPlaneRoles.bicep`) | `roles_assigned: [Contributor, User Access Administrator]` (idempotent `RoleAssignmentExists` → success) | None |

## Validation evidence

```
# Helper unit tests (Part A) — happy path / failure path / idempotent / env-var fallback / skipped
$ uv run pytest -q api/tests/test_azure_tasks.py -k "dashboard_mi"
5 passed in 3.11s

# Helper + provision integration (Part A wiring)
$ uv run pytest -q api/tests/test_azure_tasks.py api/tests/test_azure_provision_aks.py
32 passed in 2.99s

# Bicep module compiles standalone (Part C)
$ az bicep build --file infra/modules/workloadRgCreatorRole.bicep --stdout >/dev/null

# Top-level ARM regenerated with the new actions + ABAC condition
$ az bicep build --file infra/main.bicep --outfile infra/main.json
$ grep "roleAssignments/write" infra/main.json
→ present inside the workloadRgCreatorRole permissions block
$ grep "ForAnyOfAnyValues:GuidEquals" infra/main.json
→ ABAC condition rendered into the workloadRgCreatorAssignment resource
```

## Follow-up

* The `cli-upgrade.sh` preflight + `postprovision.sh` self-heal + manual
  `grant-runtime-rbac.sh` recovery path are kept as-is. They are now
  truly belt-and-suspenders for the pre-Part-C deployment cohort — once
  every active env is re-provisioned with the Part C custom role, the
  three operator helpers become no-ops (skipped: "already assigned")
  on every run.
* When adding a new role to either
  `ensure_dashboard_mi_cluster_rg_roles` or
  `setup_workload_identity`, also append the GUID to
  `allowedRoleDefinitionIds` in
  [`infra/modules/workloadRgCreatorRole.bicep`](../../../infra/modules/workloadRgCreatorRole.bicep)
  and re-run `az bicep build`. The ABAC condition will otherwise reject
  the new write the next time the MI attempts it.
