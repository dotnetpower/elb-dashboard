# 2026-05-27 — Auto-peer dashboard VNet ↔ AKS cluster VNet on `provision_aks`

## Motivation

A real env (sub `b052302c-…`, `ca-elb-dashboard` / `rg-elb-dashboard` /
`rg-elb-cluster`) hit this on 2026-05-27:

* `elb-openapi` deployment + Service + endpoints all green
  (`2/2 Running`, `endpoints: 10.244.0.60:8000, 10.244.2.196:8000`,
  internal LB IP `10.224.0.7`).
* Dashboard's API Reference page hangs at `Sending…`.
* `api` sidecar logs:
  ```
  WARNING api.routes.aks.openapi openapi/proxy: upstream request failed for http://10.224.0.7:
  ERROR   api.app.middleware req method=GET path=/api/aks/openapi/proxy status=502 elapsed=30058ms
  WARNING api.routes.aks.openapi openapi/spec:  fetch failed for http://10.224.0.7: timed out
  ```

Root cause: the AKS auto-VNet
(`aks-vnet-23268255`, in
`MC_rg-elb-cluster_elb-cluster-01_koreacentral`, `10.224.0.0/12`) has
**zero peerings** with the dashboard's platform VNet
(`vnet-elb-dashboard`). The api sidecar runs in the Container Apps
Environment subnet of `vnet-elb-dashboard`, so traffic to `10.224.0.7`
is unroutable and times out at httpx's 30 s default — the SPA sees a
502.

Token "not configured" is a separate, working-as-designed signal —
`GET /api/aks/openapi/token` returns 200 in 65 ms; the deployment just
has no `ELB_OPENAPI_API_TOKEN` env yet. The proxy code already proves
the proxy itself is OK by surfacing the empty-token case as 502 with a
distinct error code if it ever got past the network layer.

## User-facing change

* `api.tasks.azure.provision.provision_aks` now performs a
  best-effort **bidirectional VNet peering** between the dashboard
  platform VNet and the AKS-auto VNet at the end of cluster create.
  The result lands in the completion payload + return value as
  `vnet_peering: {dashboard_vnet, aks_vnet, peerings, recovery_command,
  error?, skipped?}`.
* Existing clusters created before this PR landed are not auto-fixed
  by re-provisioning (provision_aks only runs on create). For those
  there are two recovery paths, both wrapping the same helper:
  * **SPA / programmatic**: `POST /api/aks/peer-with-platform`
    `{subscription_id, resource_group, cluster_name}`. Synchronous,
    returns the peering summary. Auth: same MSAL bearer as the rest of
    `/api/aks/*`.
  * **Shell**: [`scripts/dev/peer-cluster-network.sh`](../../../scripts/dev/peer-cluster-network.sh)
    — POSTs the same payload to the deployed dashboard using the
    operator's `az login` access token, auto-detecting the Container
    App / cluster from `azd env get-values` + `az aks list`.

The peering pair uses stable names
(`peer-<local-vnet>-to-<remote-vnet>`) and the helper treats
`AlreadyExists` / `Conflict` as success, so re-runs are clean no-ops.

## Why "best-effort" and not "fail the task"

* The cluster itself is fully usable. BLAST submits through the
  terminal sidecar (which `kubectl exec`s into the AKS API server)
  work regardless of platform↔AKS VNet connectivity. The only
  capability blocked by missing peering is the API Reference
  `/api/aks/openapi/{proxy,spec}` surface.
* A pre-Part-3 dashboard MI that lacks
  `Microsoft.Network/virtualNetworks/peer/action` on the AKS-auto VNet
  would otherwise fail the entire cluster create. That trade-off is
  worse than a Try-It surface that requires one extra recovery call.
* The completion payload carries the exact recovery command so the
  SPA can render a "Network not connected — paste this" banner without
  parsing logs.

## RBAC requirements

The dashboard MI needs `Microsoft.Network/virtualNetworks/peer/action`
+ `Microsoft.Network/virtualNetworks/virtualNetworkPeerings/{read,write}`
on **both** VNets. In typical deployments:

* `vnet-elb-dashboard` lives in `rg-elb-dashboard` — covered by
  `controlPlaneRoles.bicep`'s `Contributor` grant.
* The AKS-auto VNet lives in `MC_<cluster_rg>_<cluster>_<region>`,
  which is a sub-RG of the cluster RG. The dashboard MI's `Contributor`
  on `rg-elb-cluster` (granted by the Part C custom role + ABAC, or by
  `workloadClusterRoles.bicep`, or by `grant-runtime-rbac.sh`) **does
  NOT** cascade into the MC_* RG automatically — that RG carries its
  own RBAC scope. When peering fails with `AuthorizationFailed` the
  recovery hint surfaces this and the operator can grant Network
  Contributor on the MC_* RG once.

## API / IaC diff summary

* **Python — new module** [`api/tasks/azure/peering.py`](../../../api/tasks/azure/peering.py)
  (`ensure_vnet_peering_with_cluster`, helpers) +
  `_facade._ensure_vnet_peering_with_cluster` re-export.
* **Python — wire-up** in
  [`api/tasks/azure/provision.py`](../../../api/tasks/azure/provision.py):
  new `_RBAC_SUB_PHASES` entry `ensuring_vnet_peering`, call after the
  dashboard-MI self-grant step, embed result in completion payload +
  return value.
* **Python — new route** [`api/routes/aks/peering.py`](../../../api/routes/aks/peering.py)
  `POST /api/aks/peer-with-platform`, registered in
  [`api/routes/aks/__init__.py`](../../../api/routes/aks/__init__.py).
* **Shell** [`scripts/dev/peer-cluster-network.sh`](../../../scripts/dev/peer-cluster-network.sh).
* **Tests**: new [`api/tests/test_azure_peering.py`](../../../api/tests/test_azure_peering.py)
  (10 tests — helper happy/skip/already-exists/auth-fail/env-fallback +
  route 400/200/502 + provision integration).
* **Facade contract**: added
  `api.tasks.azure.peering.ensure_vnet_peering_with_cluster` to
  `_FACADE_CONTRACT` in
  [`api/tests/test_tasks_facade_contract.py`](../../../api/tests/test_tasks_facade_contract.py).
* **No IaC change** — the dashboard VNet ID is resolved at runtime by
  stripping `/subnets/<name>` from the existing
  `PLATFORM_PRIVATE_ENDPOINT_SUBNET_ID` env var the Container App
  template already injects.

## Operator behavior matrix

| Deployment state | Auto-peer outcome | Operator action |
|---|---|---|
| New cluster created after this PR | `peerings: [{state: Connected} x 2]` | None |
| Existing cluster, MI has Network Contributor on AKS-auto VNet | hit recovery once: `POST /api/aks/peer-with-platform` (or `peer-cluster-network.sh`) | One-shot click |
| Existing cluster, MI lacks Network Contributor on AKS-auto VNet | recovery returns `error: AuthorizationFailed` + `recovery_command` | Grant Network Contributor on MC_* RG, retry |
| BYO-VNet cluster (`vnet_subnet_id` set; MC_* has no VNet) | `skipped: aks_node_rg_has_no_vnet` | None (cluster already in platform VNet or operator-managed) |
| Local dev (`PLATFORM_PRIVATE_ENDPOINT_SUBNET_ID` unset) | `skipped: dashboard_vnet_id not resolved` | None (irrelevant outside the deployed Container App) |

## Validation evidence

```
# New helper + route + provision integration
$ uv run pytest -q api/tests/test_azure_peering.py
10 passed in 2.37s

# Full backend suite (regression guard for the facade contract + existing
# provision_aks tests)
$ uv run pytest -q api/tests
1550 passed in 32.38s

# Lint
$ uv run ruff check api
All checks passed!

# Docs build (the feature note links above)
$ uv run mkdocs build --strict
INFO    -  Documentation built in 12.58 seconds
```

## Follow-up

* SPA card: surface `vnet_peering.error` + `recovery_command` on the
  API Reference / Cluster Plane card with a one-click "Connect cluster
  network" button that hits `POST /api/aks/peer-with-platform`. Backend
  is ready; UI is a separate PR.
* If a future deployment needs cross-region peering, the helper as
  written assumes same-region peering — global peering needs an extra
  `useRemoteGateways=false` + `peeringSyncLevel` consideration. Not
  blocking today because the dashboard + AKS share `AZURE_LOCATION`.
