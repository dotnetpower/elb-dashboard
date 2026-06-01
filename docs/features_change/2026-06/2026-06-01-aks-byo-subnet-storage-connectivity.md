# AKS BYO-subnet for workload Storage connectivity

## Motivation

`core_nt` DB warmup on the deployed `elb-cluster-02` failed with the dashboard
showing `AKS cache failed · 10/10`. Root cause: the AKS cluster was created in
**managed-VNet mode**, so its nodes lived in a freshly-named managed VNet
(`aks-vnet-NNNN`) that is not connected to the hub VNet hosting the workload
Storage private endpoints — no VNet peering, no private DNS zone link. Warmup
pods therefore resolved the Storage FQDN to a **public** IP, hit the account's
`publicNetworkAccess: Disabled`, and got `403 AuthorizationFailure` on the
`azcopy` manifest download.

The originally-considered fix (peer the AKS managed VNet to the hub + link the
`privatelink.*` DNS zones) is **RBAC-blocked**: the dashboard managed identity
`id-elb-dashboard-3abp67bp` has **zero** role assignments on the AKS node
resource group (`MC_rg-elb-cluster_…`), so it cannot write peering into the
managed VNet, and it cannot self-grant on that dynamically-named RG.

## User-facing change

New AKS clusters provisioned from the dashboard are created in **BYO-subnet
mode**: their nodes land in the pre-existing hub VNet subnet
`vnet-elb-dashboard/snet-aks` (10.20.4.0/23). Because the hub VNet is already
linked to the `privatelink.{blob,dfs,table}.core.windows.net` zones and the
Storage private endpoints live in the same VNet, the cluster resolves and
routes to Storage **intra-VNet** with no peering and no per-cluster wiring.
DB warmup `azcopy` now succeeds against the locked-down Storage account.

The network profile is pinned to **Azure CNI Overlay** so only nodes (not every
pod) draw IPs from `snet-aks` — 512 addresses comfortably host multiple
clusters. `publicNetworkAccess` stays `Disabled`.

**No new RBAC is required for warmup.** The MI already holds `Network
Contributor` on the platform resource group, which grants
`Microsoft.Network/virtualNetworks/subnets/join/action` on `snet-aks` — that
authorises node attachment at create time. The structural warmup fix is
RBAC-free.

**One runtime grant is added for OpenAPI**: after the cluster is created in
BYO-subnet mode, the provision task best-effort grants the cluster's
SystemAssigned control-plane identity `Network Contributor` on `snet-aks`. The
Azure cloud-provider runs the `elb-openapi` internal LoadBalancer reconcile as
the *cluster* identity (not the requesting MI), and Azure does not auto-grant
it on a cross-RG custom subnet, so without this the internal LB Service stays
`<pending>` with `AuthorizationFailed`. The grant is best-effort: warmup
`azcopy` (node outbound to the Storage private endpoint) does not need it, so a
failure only degrades OpenAPI Try-It and never fails the provision.

## API / IaC diff summary

- `api/tasks/azure/cluster_params.py`: `build_cluster_params` gains a
  `vnet_subnet_id` parameter. When truthy, both agent pools set
  `vnet_subnet_id` and an explicit overlay `ContainerServiceNetworkProfile`
  (`azure` / `overlay`, pod 10.244.0.0/16, service 10.0.0.0/16, dns 10.0.0.10).
  When falsy/None the model is unchanged (managed-VNet mode preserved for
  local/legacy callers).
- `api/tasks/azure/provision.py`: new `_resolve_aks_vnet_subnet_id()` helper
  prefers `PLATFORM_AKS_SUBNET_ID`, else derives `snet-aks` from
  `PLATFORM_PRIVATE_ENDPOINT_SUBNET_ID` (same VNet, swap the trailing subnet
  name) so the fix works on already-deployed revisions. The resolved id is
  threaded into `build_cluster_params`; managed-VNet fallback logs a warning.
- `api/tasks/azure/rbac.py`: new best-effort
  `grant_network_contributor_on_subnet(cred, subscription_id, *, principal_id,
  subnet_id)` helper (Network Contributor `4d97b98b-…`, deterministic uuid5
  assignment id, idempotent via `_create_role_assignment_with_retry`). Wired
  into `provision.py` after `poller.result()` — only in BYO-subnet mode, using
  the cluster control-plane `result.identity.principal_id`; failures log a
  warning and do not fail the task.
- `infra/modules/containerAppControl.bicep`: new `platformAksSubnetId` param,
  injected as `PLATFORM_AKS_SUBNET_ID` into the api and worker sidecars.
- `scripts/dev/postprovision.sh`: derives `platformAksSubnetId` from the
  resolved private-endpoint subnet and passes it to the Container App deploy.

The added role assignment is created at **runtime by the provision task**
(scope = the existing `snet-aks` subnet), not declared in Bicep, so the §12a
RBAC-removal preflight is unaffected.

## Validation evidence

- `uv run pytest -q api/tests/test_azure_provision_aks.py` → 17 passed (new tests: managed-VNet default, BYO-subnet + overlay pinning, `_resolve_aks_vnet_subnet_id` explicit/derive/empty, `grant_network_contributor_on_subnet` create + empty no-op).
- `uv run pytest -q api/tests` → 2371 passed, 3 skipped (no regressions).
- `uv run ruff check api` → All checks passed.
- `az bicep build --file infra/modules/containerAppControl.bicep` → BICEP_OK.
- `bash -n scripts/dev/postprovision.sh` → OK.

## Follow-up (live cluster-02 remediation — requires approval)

The code fix applies to **new** clusters. The already-running `elb-cluster-02`
is still in managed-VNet mode. Two options, both needing maintainer approval
because they touch shared infra:

- **(A) Recreate** `elb-cluster-02` in BYO-subnet mode (clean, matches the new
  design; disruptive — deletes running cluster state).
- **(B) One-time manual remediation** via the human admin `az` identity
  (the dashboard MI cannot do this): bidirectional peering
  `aks-vnet-29477498 ↔ vnet-elb-dashboard` + link `aks-vnet-29477498` to the
  three `privatelink.*` Storage zones, then Release + rewarm `core_nt`.
