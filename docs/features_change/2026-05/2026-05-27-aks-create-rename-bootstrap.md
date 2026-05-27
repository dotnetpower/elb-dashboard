# AKS create: fresh-RG-name no longer fails preflight

## Motivation

In the "+ Create AKS Cluster" modal, renaming the cluster (for example
`elb-cluster-01` → `elb-cluster-small`) derives a brand-new resource
group name (`rg-elb-cluster-small`). The dashboard managed identity
holds Contributor + User Access Administrator only on the originally
provisioned cluster RG (assigned by
[infra/modules/workloadClusterRoles.bicep](../../../infra/modules/workloadClusterRoles.bicep)),
so a freshly named RG always failed preflight with:

> Dashboard managed identity is missing 1 role assignment(s) needed for
> AKS create — Contributor missing at /subscriptions/.../resourceGroups/rg-elb-cluster-small.

That blocked submit even though the MI's sub-scope
`Elb Workload RG Creator` custom role already grants the ABAC-gated
`roleAssignments/write` needed to self-grant Contributor + UAA on any
new RG (the ABAC whitelist explicitly includes both built-in role
GUIDs — see
[infra/modules/workloadRgCreatorRole.bicep](../../../infra/modules/workloadRgCreatorRole.bicep)).

## User-facing change

- Renaming the cluster in the wizard now passes preflight as long as the
  MI holds the project's sub-scope custom role (the default in every
  `azd up` deployment since 2026-05-27 Part C).
- The RBAC row renders as **OK** with the message:
  *"Dashboard managed identity will self-grant Contributor on
  'rg-elb-cluster-small' (via the 'Elb Workload RG Creator' custom role
  at subscription scope) before AKS create — no manual role assignment
  needed."*
- No more "Fix errors above" dead-end on a typical first cluster create.

## API / IaC diff summary

### `api/services/rbac_preflight.py`

- `aks_create_rbac_check` now tracks whether sub-scope RG-write was
  satisfied via the project custom role
  (`sub_rg_write_via_custom_role`).
- When `cluster_rg_ok=False` but the custom role is present at sub
  scope, the cluster-RG requirement is reported as
  **bootstrap-capable** (`status="ok"`) instead of `fail`.
- `details.cluster_rg_bootstrap_capable: bool` is added to the row so
  the FE / future tooling can distinguish "pre-granted" from
  "self-granted-at-submit".
- No change when the custom role is absent — that path still returns
  `fail` with the same `missing[]` shape consumed by
  `ProvisionModal.tsx`.

### `api/tasks/azure/provision.py`

- After `ensuring_resource_group` (RG create + visibility wait) and
  before `arm_create_or_update`, the task now calls
  `_ensure_dashboard_mi_cluster_rg_roles` to self-grant Contributor +
  UAA on the cluster RG.
- This is idempotent (stable UUIDs), so the existing post-create
  self-grant becomes a no-op on success.
- On failure of the pre-create grant we log a warning and fall
  through — the existing per-RG Contributor (typically assigned by
  `workloadClusterRoles.bicep` for the default RG) still works.

No IaC change. The `Elb Workload RG Creator` ABAC whitelist already
includes Contributor + UAA, so no Bicep churn is needed.

## Validation evidence

- `uv run pytest -q api/tests/test_rbac_preflight.py
  api/tests/test_azure_provision_aks.py api/tests/test_azure_tasks.py`
  → 46 passed.
- `uv run pytest -q api/tests` → **1625 passed** (full sweep).
- `uv run ruff check api/services/rbac_preflight.py
  api/tasks/azure/provision.py api/tests/test_rbac_preflight.py`
  → clean.
- `cd web && npx tsc --noEmit -p tsconfig.json` → clean (FE consumes
  the new `details` field through `Record<string, unknown>`; no API
  type change required).
- New unit tests:
  - `test_rbac_check_ok_when_only_custom_role_at_sub_bootstraps_cluster_rg`
    — exercises the "renamed cluster → fresh RG" path.
  - `test_rbac_check_fail_when_no_sub_scope_grants_at_all` — guards
    that the fail path still triggers when neither sub-scope
    Contributor nor the custom role is present.
