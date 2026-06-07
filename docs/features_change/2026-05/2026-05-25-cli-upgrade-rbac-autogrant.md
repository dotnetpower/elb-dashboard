# 2026-05-25 — Auto-grant runtime RBAC on AKS cluster RG

## Motivation

Both production environments (env A `ca-elb-dashboard-01` and env B
`ca-elb-dashboard`) shipped with a **structural gap** in their deployed
RBAC:

* [`infra/modules/controlPlaneRoles.bicep`](../../../infra/modules/controlPlaneRoles.bicep)
  grants the shared dashboard managed identity `Contributor` +
  `User Access Administrator` on **the deployment resource group only**
  (`resourceGroup()` scope).
* The OpenAPI deploy task
  [`api/tasks/openapi/rbac.py::setup_workload_identity`](../../../api/tasks/openapi/rbac.py)
  reaches into the **AKS cluster's RG** (`rg-elb-cluster`) and tries to:
  1. Create the workload MI `id-elb-openapi`
  2. Create a Federated Identity Credential under it
  3. Assign `Contributor` / `Storage Blob Data Contributor` /
     `Azure Kubernetes Service Cluster User Role` to that MI
  4. `az aks get-credentials --admin` + `kubectl apply` the manifests

Without `Contributor` + UAA on `rg-elb-cluster`, step 1 immediately
fails with:

```
azure.core.exceptions.HttpResponseError: (AuthorizationFailed)
The client 'e4f4e63d-…' with object id '3f06c475-…' does not have
authorization to perform action
'Microsoft.ManagedIdentity/userAssignedIdentities/write' over scope
'/subscriptions/…/resourceGroups/rg-elb-cluster/providers/Microsoft.ManagedIdentity/userAssignedIdentities/id-elb-openapi'
```

and the SPA shows
`workload identity setup failed; OpenAPI pod would have no AZURE_CLIENT_ID.`

Discovered today on env B (`ca-elb-dashboard`) when an operator clicked
"Deploy elb-openapi". Env A had the same gap (zero role assignments on
`rg-elb-cluster` for both dashboard MIs).

## User-facing change

* **New helper** [`scripts/dev/grant-runtime-rbac.sh`](../../../scripts/dev/grant-runtime-rbac.sh)
  — workstation-driven, idempotent grant of `Contributor` +
  `User Access Administrator` on the AKS cluster RG to the deployed
  dashboard MI. Auto-detects the MI principal id from
  `az containerapp show` and the AKS cluster RG from `az aks list`
  (unambiguous "exactly one" case). Supports `--dry-run`, `--yes`,
  `--container-app`, `--rg`, `--subscription`, `--cluster-rg`,
  `--principal-id` overrides.
* **`cli-upgrade.sh` now runs the grant as a preflight step**
  (best-effort: a failure logs a recovery hint but does not block the
  api/frontend/terminal image rollout itself). Add `--skip-rbac-grant`
  to bypass.
* Documented in [`docs/operate/cli-upgrade.md`](../../operate/cli-upgrade.md)
  preflight checklist.

## API / IaC diff summary

* No API change — pure operational/ops tooling + IaC.
* **Bicep**: new sibling module
  [`infra/modules/workloadClusterRoles.bicep`](../../../infra/modules/workloadClusterRoles.bicep)
  + two new `main.bicep` params (`aksClusterResourceGroup`,
  `assignWorkloadClusterRoles`). When `aksClusterResourceGroup` is set
  (e.g. `rg-elb-cluster`), `azd provision` now grants the same
  `Contributor` + `User Access Administrator` pair on the AKS cluster
  RG that `controlPlaneRoles.bicep` already grants on the deployment
  RG. The assignment `name` is `guid(resourceGroup().id,
  uamiPrincipalId, roleId)` so a redeploy against an RG where the
  helper already created the assignment is a no-op (Bicep adopts the
  existing assignment in place).
* **`postprovision.sh`** now calls `grant-runtime-rbac.sh` as a
  best-effort self-heal at the end of `azd provision` — covers the
  "first `azd up` had no AKS RG yet, SPA created AKS later, never
  re-ran `azd provision`" case.
* Runtime contract preserved: `api/tasks/openapi/rbac.py` and
  `api/tasks/openapi/deploy.py` already raise loudly with the exact
  error string the SPA displays; nothing in those files changed.

## Manual recovery applied to running environments

Both env A and env B already had the gap before this PR. The new helper
was run against both:

```bash
bash scripts/dev/grant-runtime-rbac.sh --yes \
  --container-app ca-elb-dashboard-01 --rg rg-elb-dashboard-01 \
  --subscription 00000000-0000-0000-0000-0000000000a1
# created=2 skipped=0 failed=0  (Contributor, User Access Administrator)

bash scripts/dev/grant-runtime-rbac.sh --yes \
  --container-app ca-elb-dashboard --rg rg-elb-dashboard \
  --subscription 00000000-0000-0000-0000-0000000000a1
# created=2 skipped=0 failed=0
```

Both MIs now show the expected pair on `rg-elb-cluster`:

```
$ az role assignment list --assignee <oid> --scope /subscriptions/.../resourceGroups/rg-elb-cluster --query "[].roleDefinitionName" -o tsv
Contributor
User Access Administrator
```

(Env A MI oid `2fef9815-d8ac-4956-bbdb-1bf937392b30`,
env B MI oid `3f06c475-95ee-45f3-85e8-751f740e123f`.)

## Validation evidence

```
# bash syntax + dry-run from clean tree (env A azd default)
bash -n scripts/dev/grant-runtime-rbac.sh && echo syntax OK
bash -n scripts/dev/cli-upgrade.sh && echo syntax OK

bash scripts/dev/grant-runtime-rbac.sh --dry-run --yes \
  --container-app ca-elb-dashboard-01 --rg rg-elb-dashboard-01 \
  --subscription 00000000-0000-0000-0000-0000000000a1
# Subscription:    00000000-0000-0000-0000-0000000000a1
# Container App:   ca-elb-dashboard-01 (rg-elb-dashboard-01)
# Dashboard MI:    2fef9815-d8ac-4956-bbdb-1bf937392b30
# AKS cluster RG:  rg-elb-cluster
# (dry-run — no role assignments will be created)
#   [dry ] would assign Contributor at /subscriptions/.../resourceGroups/rg-elb-cluster
#   [dry ] would assign User Access Administrator at /subscriptions/.../resourceGroups/rg-elb-cluster

bash scripts/dev/cli-upgrade.sh api --dry-run --allow-dirty --yes 2>&1 | grep grant-runtime
# ==> (dry-run) would call grant-runtime-rbac.sh --container-app ca-elb-dashboard-01 --rg rg-elb-dashboard-01
```

Re-running the helper after the manual recovery (round-trip idempotency):

```
$ bash scripts/dev/grant-runtime-rbac.sh --yes \
    --container-app ca-elb-dashboard --rg rg-elb-dashboard \
    --subscription 00000000-0000-0000-0000-0000000000a1
[skip] Contributor already assigned at /subscriptions/.../resourceGroups/rg-elb-cluster
[skip] User Access Administrator already assigned at /subscriptions/.../resourceGroups/rg-elb-cluster
Summary: created=0 skipped=2 failed=0
```

## Follow-up

1. ~~**Bicep follow-up**: extend `infra/modules/controlPlaneRoles.bicep`
   (or add a sibling module `workloadClusterRoles.bicep`) so new
   deployments grant the workload-cluster-RG roles at provision time
   and `grant-runtime-rbac.sh` becomes a pure self-healing safety net.~~
   **Done in this PR** — see [`infra/modules/workloadClusterRoles.bicep`](../../../infra/modules/workloadClusterRoles.bicep)
   + the `aksClusterResourceGroup` / `assignWorkloadClusterRoles`
   params in `infra/main.bicep`. The shell helper is now formally the
   self-healing safety net (also invoked from `postprovision.sh`).
2. To opt new deployments into the Bicep grant after AKS exists, set:
   ```bash
   azd env set AKS_CLUSTER_RESOURCE_GROUP rg-elb-cluster
   azd provision
   ```
   The new `aksClusterResourceGroup` Bicep param is wired into
   `infra/main.parameters.json` as `${AKS_CLUSTER_RESOURCE_GROUP=}` so
   the standard azd env-to-bicep binding picks it up. When the env var
   is unset the module is skipped (default empty string). The
   `postprovision.sh` self-heal call covers the case where operators
   forget to set it.
3. RBAC propagation typically takes 1-5 min after `az role assignment
   create`. If the SPA's "Deploy elb-openapi" still returns
   `AuthorizationFailed` immediately after the grant, retry after ~2 min.
