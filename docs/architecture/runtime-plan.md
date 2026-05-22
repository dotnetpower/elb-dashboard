---
title: Runtime Plan — Networking, Identity, Storage, AKS
description: Operator reference for the supporting infrastructure of the ElasticBLAST Control Plane Container App — VNet subnets, private DNS, shared managed identity + RBAC, Storage account rules, AKS plan, and the post-deploy smoke checklist.
tags:
  - architecture
  - infra
  - security
---

# Runtime Plan — Networking, Identity, Storage, AKS

This page is the *operator* reference for the supporting Azure infrastructure
that the [Container Apps Architecture](container-apps.md) depends on. The
container app itself is described there; this page covers everything that
*surrounds* it.

If you are looking for the load-bearing security contract on Storage and the
browser ↔ Storage proxy, see [Storage Network Isolation & Browser ↔ Storage Proxy](storage-contract.md).

## Networking Plan

Use one platform VNet with purpose-specific subnets.

| Subnet | Purpose |
|--------|---------|
| `snet-containerapps` | Container Apps Environment infrastructure (the single `ca-elb-dashboard` app and its six sidecars). |
| `snet-private-endpoints` | Private endpoints for Key Vault, Storage (blob + table + file), and ACR. |
| `snet-aks` | AKS nodes when the workload cluster is created by this platform. |

No `snet-redis` subnet: Redis runs as a sidecar inside the Container App and
is bound to `127.0.0.1` only.

No `snet-terminal` and no `snet-bastion` subnet: there is no Remote Terminal
VM and no Bastion. The browser shell is the `terminal` sidecar, reached via
the api sidecar's authenticated WebSocket proxy.

Private DNS zones:

- `privatelink.vaultcore.azure.net`
- `privatelink.blob.core.windows.net`
- `privatelink.table.core.windows.net`
- `privatelink.azurecr.io`

(No `privatelink.file.core.windows.net` — the control plane does not mount
Azure Files. No `privatelink.servicebus.windows.net` and no Cosmos/PostgreSQL
DNS zones.)

Network rules:

- Key Vault `publicNetworkAccess` is `Disabled` from day 1; reachable only via
  its private endpoint.
- Platform Storage `publicNetworkAccess` is `Disabled` from day 1. Reachable
  only via blob and table private endpoints in `snet-private-endpoints`.
- Workload Storage `publicNetworkAccess` is `Disabled` from day 1. AKS reaches
  it through the blob (and dfs, if HNS) private endpoints because AKS nodes
  run in `snet-aks` in the same VNet. The terminal sidecar reaches workload
  storage from `snet-containerapps` over the same private endpoint.
- The previous temporary-public-access window for ElasticBLAST (auto-enable
  -> wait -> auto-disable) is **removed**. There is no operational state in
  which any in-scope storage account is publicly reachable.
- ACR `publicNetworkAccess` is `Disabled` once private endpoint is verified
  from the Container App and AKS.
- No public SSH path exists in the final design because there is no Remote
  Terminal VM. The browser shell is reached only through the api sidecar's
  authenticated WebSocket proxy.
- Restrict AKS API access with private cluster or authorized IP ranges.

## Identity and RBAC Plan

Use user-assigned managed identities so identities survive app recreation and
can be referenced cleanly from Bicep.

| Identity | Assigned to | Required scopes |
|----------|-------------|-----------------|
| `id-elb-dashboard-*` | `ca-elb-dashboard` Container App (shared by all six sidecars including `frontend` and `terminal`) | Contributor plus User Access Administrator on workload RGs; Storage Table Data Contributor + Storage Blob Data Contributor on platform storage; data-plane roles on workload Storage and ACR; Key Vault Secrets User; AcrPull on the platform ACR; AKS RBAC reader / `Azure Kubernetes Service Cluster User` so the terminal sidecar can run `kubectl` against the cluster. The `frontend` sidecar makes no Azure calls and inherits the MI only because it lives in the same revision. |
| `id-elb-openapi` | AKS Workload Identity | Storage Blob Data Contributor, AKS permissions, workload RG permissions needed by ElasticBLAST. |

Because the six sidecars share one MI, the api sidecar technically holds the
same ARM Contributor rights as the worker, the terminal, and the frontend.
Scope abuse is mitigated by:

- Mutating ARM operations only run inside Celery task handlers (in the
  worker process) or as user-typed shell commands inside the terminal sidecar
  (which is gated by MSAL + tenant role at the WebSocket upgrade).
- The api sidecar's request handlers do not call ARM mutation methods; this
  is enforced by static analysis (allow-list of Azure SDK call sites per
  sidecar package).
- The frontend sidecar is `nginx:alpine` with no Azure SDK and no shell; it
  cannot use the MI even if it wanted to.

A future split into separate Container Apps would re-introduce per-process
identities; this is an explicit, documented compromise in exchange for the
cost saving.

Keep the browser token as proof of caller identity. Do not exchange or persist
the token in Celery task arguments. Store `owner_oid`, `tenant_id`, and approved
operation parameters in the Storage state row. The worker sidecar uses the
shared managed identity (`id-elb-dashboard-*`) for all Azure operations.

## Storage Plan

Storage has three roles:

1. **Platform state storage** for the control plane: job registry table, audit
   append blobs, schedule definitions, dead-letter records, and command
   history. No Azure Files shares — `redis` and `terminal` sidecars are
   ephemeral and the broker queue is rebuilt from the `jobstate` table by
   the `beat` reconciler on revision restart.
2. **ElasticBLAST workload storage** for `blast-db`, `queries`, and `results`.
3. **Operational artifacts**: container release zips, diagnostic dumps.

Target rules:

- Use managed identity and Azure RBAC; do not use shared keys
  (`allowSharedKeyAccess: false`).
- **Every Storage account in scope (platform + workload) has
  `publicNetworkAccess: Disabled`, `networkAcls.defaultAction: Deny`, and
  `bypass: None`. This is enforced from creation, not as a later hardening
  step. See [Storage Isolation & Proxy](storage-contract.md) for the full
  requirement set and verification steps.**
- Keep HNS enabled on workload storage when ElasticBLAST needs it. Platform
  state storage does **not** need HNS.
- Keep containers private.
- Generate **no** SAS for browser-facing flows. All browser uploads and
  downloads go through the api sidecar as a streaming proxy. See the
  [Browser ↔ Storage Proxy](storage-contract.md#browser-storage-proxy-no-sas-to-the-browser)
  contract, chunk sizes, concurrency limits, and verification tests.
- Store DB preparation progress in the platform state table, not background
  threads.
- For large NCBI database imports, the worker downloads through the private
  Storage endpoint. Server-side copy is not relied upon if the source forces
  public-only access.
- Apply lifecycle policies on `dead-letter` and `audit` blobs (e.g. cool tier
  after 30 days, delete after 365 days) to bound cost.

## AKS Plan

AKS remains the compute plane for ElasticBLAST.

Target rules:

- Keep OIDC issuer and Workload Identity enabled.
- Keep Blob CSI driver enabled if BLAST DB access depends on it.
- Prefer private cluster for production environments.
- If a private cluster is not feasible, configure authorized IP
  ranges and audit the exception.
- Replace public `elb-openapi` LoadBalancer with an internal service or ingress
  once the Container Apps Environment and AKS can communicate privately.
- Continue to surface AKS node, pod, warmup, and job state through API routes;
  do not make the browser talk to AKS directly.

## Post-deploy Smoke Checklist (RBAC + discovery)

The single most common production regression is the SPA's discovery
wizard rendering an empty list because the shared UAMI cannot reach ARM
control-plane LIST operations. Run this after every `azd provision` or
after any change to identity / role assignments.

1. **MI is attached to the Container App.**
   ```bash
  az containerapp show --name ca-elb-dashboard --resource-group rg-elb-dashboard \
     --query 'identity'
   ```
   Expect `type: UserAssigned` and the UAMI's resource id under
   `userAssignedIdentities`.

2. **`AZURE_CLIENT_ID` env matches the UAMI's clientId on the api sidecar.**
   ```bash
  az containerapp show --name ca-elb-dashboard --resource-group rg-elb-dashboard \
     --query "properties.template.containers[?name=='api'].env[?name=='AZURE_CLIENT_ID'].value"
   ```
   Should be the UAMI's `clientId` (not the app registration id, not zeros).

3. **One-shot end-to-end probe.** Curl the api ingress (auth-gated —
   the response references subscription ids so it is hidden behind
   the standard MSAL bearer):
   ```bash
   TOKEN=$(az account get-access-token --resource api://<api-app-client-id> \
     --query accessToken -o tsv)
   curl -fsS -H "Authorization: Bearer $TOKEN" \
     https://<ingress-fqdn>/api/health/azure-discovery | jq
   ```
   All three steps (`credential`, `subscriptions_list`,
   `resource_groups_list`) must report `status: ok`. If any step is
   `error` or `subscriptions_list.count_capped_at_5` is `0`, the
   `hint` field tells you exactly which `az role assignment create`
   to run. Subscription ids in the response are masked (`b0523…`)
   and display names are dropped. The probe is read-only; do not
   poll it from a dashboard.

4. **Subscription-scope Reader is in place.** Bicep
   (`infra/modules/subscriptionRoles.bicep`)
   does this automatically when `assignSubscriptionReader=true`
   (the default). If the deployer lacks `User Access Administrator`,
   the deployment fails the role assignment with a 403; recover by:
   ```bash
   az role assignment create --role Reader \
     --scope /subscriptions/<sub> \
     --assignee-object-id <uami-objectId> \
     --assignee-principal-type ServicePrincipal
   ```
   then re-run `azd provision` (the bicep is idempotent).

5. **Logs.** All `/api/arm/list_*` failures now log the exception type,
   sanitised message, and traceback. Tail the api sidecar:
   ```bash
  az containerapp logs show --name ca-elb-dashboard --resource-group rg-elb-dashboard \
     --container api --follow | grep -iE 'list_(subscriptions|resource_groups|storage|acrs|vms)'
   ```
   A repeated `AuthorizationFailed` line is the smoking gun for missing
   sub-scope Reader.

The local-compose equivalent of this failure mode (no host `az login`
mounted) is documented in [docs/features_change/2026-05/2026-05-15-dev-compose-az-cli-mount.md](../features_change/2026-05/2026-05-15-dev-compose-az-cli-mount.md).

## See also

- [Container Apps Architecture](container-apps.md) — the bundled six-sidecar app this plan supports.
- [Storage Isolation & Proxy](storage-contract.md) — the non-negotiable Storage security contract.
- [Authentication & Authorization](authentication.md) — MSAL + managed identity that gates every Azure call.
