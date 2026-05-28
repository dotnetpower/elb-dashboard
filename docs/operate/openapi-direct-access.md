---
title: External direct access to elb-openapi
description: Expose the ``elb-openapi`` AKS Service to callers that do not route through the dashboard — either via [VNet peering](https://learn.microsoft.com/azure/virtual-network/virtual-network-peering-overview) (same tenant, no overlapping CIDR) or [AKS-managed Private Link Service](https://learn.microsoft.com/azure/aks/internal-lb#expose-an-internal-load-balancer-using-azure-private-link-service) (different subscription, overlapping CIDR, or three+ VNets).
tags:
  - operate
  - blast
---

# External direct access to ``elb-openapi``

`elb-openapi` runs as a Kubernetes Service of type `LoadBalancer` with the
[`azure-load-balancer-internal`](https://learn.microsoft.com/azure/aks/internal-lb) annotation, so by default
it gets an ILB IP inside the AKS load-balancer subnet. The dashboard
reaches the ILB IP from its own VNet (via the deployment-time peering
between dashboard VNet and the AKS LB VNet). When a 3rd-party caller (a
JupyterHub instance, a notebook VM, a partner subscription) needs to call
`/v1/jobs` directly without routing through the dashboard, you have two
options.

## Option A — VNet peering (recommended when feasible)

**Use when:** caller VM lives in the same tenant, in a VNet whose CIDR
does **not** overlap the AKS LB VNet, and you control both VNets.

1. Capture the AKS LB VNet name + resource group:
   ```bash
   az aks show -g $RG -n $CLUSTER --query "networkProfile.loadBalancerSku, agentPoolProfiles[0].vnetSubnetId" -o tsv
   ```
2. Create peering both ways:
   ```bash
   az network vnet peering create \
     --name caller-to-aks --resource-group $CALLER_RG \
     --vnet-name $CALLER_VNET --remote-vnet $AKS_VNET_ID --allow-vnet-access
   az network vnet peering create \
     --name aks-to-caller --resource-group $AKS_VNET_RG \
     --vnet-name $AKS_VNET --remote-vnet $CALLER_VNET_ID --allow-vnet-access
   ```
3. From the caller VM, point at the ILB IP returned by
   `kubectl get svc elb-openapi -o jsonpath='{.status.loadBalancer.ingress[0].ip}'`.
4. Validate:
   ```bash
   curl -sS -H "X-ELB-API-Token: $TOKEN" http://$ILB_IP/v1/ready
   ```
   Expect `{"ready": true, "checks": {...}}`.

**Pros:** ~zero added latency, no extra Azure resources, no per-connection
cost. **Cons:** requires non-overlapping CIDRs and the same tenant.

## Option B — AKS-managed Private Link Service (PLS)

**Use when:** caller VM is in a different subscription / tenant, or your
caller VNet's CIDR overlaps the AKS LB VNet, or you want to expose the API
to many VNets without N×N peering.

1. On the dashboard host (or the Container App `api` sidecar) set these
   env vars **before** triggering "Deploy elb-openapi" from the SPA:
   ```bash
   export OPENAPI_PLS_ENABLED=true
   export OPENAPI_PLS_NAME=pls-elb-openapi          # defaults to this
   export OPENAPI_PLS_LB_SUBNET=snet-elb-lb         # NO default — must be set
   export OPENAPI_PLS_VISIBILITY='*'                # or 'sub-aaaa,sub-bbbb'
   export OPENAPI_PLS_AUTO_APPROVAL='sub-aaaa'      # optional
   ```
   `OPENAPI_PLS_LB_SUBNET` is the subnet **inside the AKS LB VNet** that
   the PLS NIC will live in. The AKS cloud-provider controller refuses
   to use the same subnet as the LB itself; provision a small dedicated
   one (e.g. `/29`) before enabling.

2. **First-time activation only.** AKS honours the
   `azure-pls-*` annotations only on Service *create*. If the cluster
   already has an ILB-only `elb-openapi` Service, the deploy task will
   refuse with status `blocked` / code `openapi_pls_recreate_required`.
   To accept the ~1–2 min ingress outage required to recreate the
   Service, set:
   ```bash
   export OPENAPI_PLS_CONFIRM_RECREATE=1
   ```
   …then re-trigger "Deploy elb-openapi". The deploy task will
   `kubectl delete svc elb-openapi` first, then re-apply with the PLS
   annotations. **Unset this env var again immediately after the
   activation succeeds** so a routine re-deploy can't repeat the outage
   by accident.

3. Find the PLS alias and create a Private Endpoint in the caller's
   subscription:
   ```bash
   PLS_ID=$(az network private-link-service list -g $AKS_NODE_RG \
            --query "[?name=='$OPENAPI_PLS_NAME'].id" -o tsv)
   az network private-endpoint create \
     --name pe-elb-openapi --resource-group $CALLER_RG \
     --vnet-name $CALLER_VNET --subnet $CALLER_SUBNET \
     --private-connection-resource-id $PLS_ID \
     --connection-name elb-openapi --manual-request false
   ```
4. From the caller VM, point at the Private Endpoint NIC's IP. Validate
   the same way as Option A.

**Pros:** isolated, cross-subscription, no peering / CIDR constraints.
**Cons:** ~$10/mo per PLS + per-GB egress, ~1 ms added latency, requires
the first-time recreate step above.

## Authentication and token rotation

All `/v1/*` routes (except `/v1/health` and `/v1/ready`) require the
`X-ELB-API-Token` header. The token is the same on both access paths.

* Initial token: minted by the first "Deploy elb-openapi" run and stored
  in the dashboard's runtime cache.
* Rotation: SPA → API Reference → "Generate new token". This triggers
  `POST /api/aks/openapi/token`, mints a new token, restarts the pod with
  the new env, and updates the cache.
* The new token is shown **once** in the SPA toast. Capture it and update
  any direct caller before the toast disappears; the old token is
  revoked immediately on pod restart.

## Troubleshooting

| Symptom from `curl http://$IP/v1/ready` | Likely cause | Fix |
|---|---|---|
| TCP timeout | No network path | Check peering / PE state, confirm caller subnet NSG allows egress to the LB / PE NIC IP |
| TCP RST | LB has no backend pod ready | `kubectl get pod -l app=elb-openapi` — fix scheduling / image-pull |
| `401 Unauthorized` | Stale token | Rotate via SPA, update caller |
| `503` `{"code": "k8s_unreachable"}` | AKS API server down or unreachable from the pod | Check AKS cluster state / private cluster reachability |
| `503` `{"code": "no_workload_nodes"}` | BLAST node pool scaled to zero / wrong label | `kubectl get nodes -l workload=blast` — scale up / fix label |
| `503` `{"code": "openapi_pod_not_ready"}` | Pod crash-looping or stuck | `kubectl logs deploy/elb-openapi --previous`, check the deploy task's diagnostic events |
| `502 Bad Gateway` | LB cannot reach the pod (NSG / probe failing) | Check the LB health probe config; verify pod container port 8000 |

## Related charter

* §9 "Storage Network Isolation" — PLS for `elb-openapi` does **not**
  change Storage exposure. Storage stays
  [`publicNetworkAccess: Disabled`](https://learn.microsoft.com/azure/storage/common/storage-network-security)
  for both peered and PLS callers.
* The dashboard's own `/api/v1/elastic-blast/submit` route also
  pre-flights `/v1/ready` before each submit. Direct callers should do
  the same to avoid the 90 s submit timeout on cold-started clusters.
