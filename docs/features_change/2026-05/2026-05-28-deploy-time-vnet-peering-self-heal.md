# Deploy-time VNet peering self-heal

**Date:** 2026-05-28
**Type:** infra / scripts
**Files touched:** `scripts/dev/postprovision.sh`, `scripts/dev/grant-runtime-rbac.sh`, `scripts/dev/peer-cluster-network.sh`, `deploy.sh`

## Motivation

External-cluster join flow (the `ELB_CLUSTER_RG_NAME` path, where the dashboard
peers with an AKS cluster created outside `azd up`) left two things manual:

1. The dashboard MI never received `Network Contributor` on the AKS-auto VNet
   in `MC_<cluster_rg>_<cluster>_<region>`. Without it, the platform-side peer
   creation passes but the cluster-side mirror call fails with
   `LinkedAuthorizationFailed` /
   `Microsoft.Network/virtualNetworks/peer/action … not authorized`.
2. Even with RBAC in place, no automation triggered the actual peering. The
   first time the SPA tried to reach the cluster's elb-openapi endpoint the
   user got `openapi_upstream_unreachable: missing VNet peering …` and had to
   curl `peer-cluster-network.sh` by hand.

This produced a recurring "first click after deploy fails" experience that the
existing self-heal pattern (`grant-runtime-rbac.sh` already called from
postprovision) was meant to prevent.

## User-facing change

* `azd up` / `azd provision` now do **both** of these end-to-end:
  - Grant `Network Contributor` on the AKS-auto VNet in addition to
    `Contributor` + `User Access Administrator` on the cluster RG.
  - Create / verify bidirectional VNet peering between the dashboard platform
    VNet and the cluster's aks-auto VNet.
* New opt-out: `ELB_SKIP_AUTO_PEER=true` skips only the peering call (the RBAC
  grant always runs because it's the prerequisite). Use when an external
  operator manages peering out of band, or on first `azd up` before any AKS
  cluster exists.
* `peer-cluster-network.sh` now resolves the target AKS cluster via the same
  `managedBy=elb-dashboard` + `azd-env-name` tag filter that
  `grant-runtime-rbac.sh` uses, so subscriptions with multiple AKS clusters
  (dev / test / prod side-by-side) no longer require explicit
  `--cluster-name` / `--cluster-rg` flags for the common case.

## Diff summary

### `scripts/dev/grant-runtime-rbac.sh`
* Header docstring updated to mention Network Contributor on the aks-auto VNet
  and the peering rationale.
* `ASSIGNMENTS` array construction moved **before** the plan-summary +
  confirmation prompt so the operator sees all three roles up front.
* New dynamic-resolution block (only outside bootstrap mode): for every AKS
  cluster in `CLUSTER_RG`, look up `nodeResourceGroup`, find the aks-auto
  VNet via `az network vnet list`, and append a `Network Contributor` entry
  scoped to that VNet's ARM id. BYO-VNet clusters (no VNet in the node RG)
  are skipped with an `[info]` line — no failure, no false-positive grant.

### `scripts/dev/peer-cluster-network.sh`
* JMESPath flatten fix (committed earlier in session): the original
  `containers[?name=='api'].env[?name=='X']` always returned `[]`; replaced
  with `containers[?name=='api'].env[] | [?name=='X']` for both
  `API_CLIENT_ID` and `PLATFORM_PRIVATE_ENDPOINT_SUBNET_ID` queries.
* AKS cluster auto-detect upgraded to a 4-step precedence:
  1. `--cluster-name` + `--cluster-rg` flags (explicit operator intent).
  2. `ELB_CLUSTER_RG_NAME` (env or azd env) — pick the single cluster in
     that RG.
  3. Tag filter: `managedBy=elb-dashboard`, narrowed by `azd-env-name` when
     present. Mirrors `grant-runtime-rbac.sh`.
  4. Legacy fallback: subscription has exactly one AKS cluster.
* Multi-match still refuses with rc=3 + the matching cluster list, suggesting
  `--cluster-name`/`--cluster-rg` or `ELB_CLUSTER_RG_NAME`.

### `scripts/dev/postprovision.sh`
* New Section 5 (after the existing Section 4 RBAC self-heal): if
  `ELB_SKIP_AUTO_PEER` is not `true`, invoke
  `scripts/dev/peer-cluster-network.sh --yes` against the freshly-deployed
  Container App. Output is indented under `    `. Exit-code handling:
  - `0` → "✓ VNet peering OK".
  - `3` → "ⓘ skipped (no AKS cluster yet, or ambiguous)" with hint to pass
    `--cluster-name` + `--cluster-rg`.
  - `2` → "⚠ partial — re-run", typically transient RBAC-propagation race.
  - other → manual recovery hint.
* Best-effort: failure never breaks the deploy.

### `deploy.sh`
* New `ELB_SKIP_AUTO_PEER` env var documented in the usage block right after
  `ELB_CLUSTER_RG_REGION`.

## API / IaC impact

* No Python, Bicep, or frontend code changed.
* No new dependencies.
* Backward compatible:
  - `grant-runtime-rbac.sh` still works in bootstrap mode (cluster RG missing
    → skip the new resolver block). Dry-run output unchanged for that case.
  - `peer-cluster-network.sh` retains its existing CLI: the recovery commands
    rendered by `api/tasks/azure/peering.py` (which always pass explicit
    `--cluster-rg --cluster-name --subscription`) take the same path as
    before.

## Validation evidence

### Syntax (`bash -n`)
```
$ bash -n scripts/dev/grant-runtime-rbac.sh && echo OK
OK
$ bash -n scripts/dev/postprovision.sh && echo OK
OK
$ bash -n scripts/dev/peer-cluster-network.sh && echo OK
OK
$ bash -n deploy.sh && echo OK
OK
```

### `grant-runtime-rbac.sh --dry-run`
```
[…] Container App:   ca-elb-dashboard (rg-elb-dashboard)
[…] Dashboard MI:    e51aaab3-eb17-4935-a7eb-446b53a5c445
[…] AKS cluster RG:  rg-elb-cluster
[…] Plan (3 role assignment(s)):
[…]   - Contributor @ /subscriptions/…/resourceGroups/rg-elb-cluster
[…]   - User Access Administrator @ /subscriptions/…/resourceGroups/rg-elb-cluster
[…]   - Network Contributor @ /subscriptions/…/resourceGroups/mc_rg-elb-cluster_elb-cluster-01_koreacentral/providers/Microsoft.Network/virtualNetworks/aks-vnet-23268255
(dry-run — no role assignments will be created)
  [skip] Contributor already assigned at …/rg-elb-cluster
  [skip] User Access Administrator already assigned at …/rg-elb-cluster
  [dry ] would assign Network Contributor at …/Microsoft.Network/virtualNetworks/aks-vnet-23268255

[…] Summary: created=0 skipped=2 failed=0
```
Confirms: Network Contributor on the aks-auto VNet is the only missing grant
in the live environment — exactly the gap that caused
`openapi_upstream_unreachable` today.

### `peer-cluster-network.sh --dry-run --yes` (no explicit cluster flags)
Subscription had 5 AKS clusters; tag filter resolved correctly to the
elb-dashboard cluster without forcing the operator to specify it:
```
[…] Container App:   ca-elb-dashboard (rg-elb-dashboard)
[…] AKS cluster:     elb-cluster-01 (rg=rg-elb-cluster)
[…] Endpoint:        POST https://ca-elb-dashboard.…/api/aks/peer-with-platform
  [dry ] would POST … with cluster_name=elb-cluster-01 resource_group=rg-elb-cluster
```

### Self-review notes
* Consumer search confirmed no Python or test caller invokes the scripts
  at runtime — they only embed the script name in recovery-command strings.
  All such callers (`api/tasks/azure/peering.py`,
  `api/tasks/azure/rbac.py`, `api/routes/aks/openapi.py`,
  `api/tests/test_azure_peering.py`, `api/tests/test_azure_tasks.py`) keep
  passing explicit `--cluster-rg --cluster-name --subscription`, so the new
  tag-based auto-detect path is purely additive.
* No `.py` / `.ts` / `.tsx` / `.bicep` files modified — pytest, ruff, and
  `npm run build` are not required for this change set.
* `git status --short` shows only the four intended files dirty among the
  scripts touched here (plus pre-existing unrelated WIP).

## Manual rollback

* `ELB_SKIP_AUTO_PEER=true ./deploy.sh` skips the new peering step.
* To remove the Network Contributor grant:
  `az role assignment delete --assignee <MI principalId> --role "Network Contributor" --scope <aks-vnet ARM id>`.
* To remove the peering:
  `az network vnet peering delete -g <platform_rg> --vnet-name <platform_vnet> -n <peer_name>`
  + the mirror on the aks-auto VNet side.
