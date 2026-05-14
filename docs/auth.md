# Authentication & Authorization

This document describes the authentication flow and every RBAC role
required by the ElasticBLAST control plane.

---

## Architecture Overview

```
Browser (SPA)               Function App                    Azure Resources
┌──────────┐  Bearer JWT    ┌──────────────┐  Managed ID    ┌──────────────┐
│ MSAL.js  │───────────────>│ Validate JWT │───────────────>│ ARM / Data   │
│ Auth Code │                │ (who called) │  DefaultAzure  │ Plane APIs   │
│ + PKCE   │                │              │  Credential()  │              │
└──────────┘                └──────────────┘                └──────────────┘
```

**Key design choice**: All Azure SDK calls use the **Function App's
system-assigned Managed Identity (MI)**, not On-Behalf-Of (OBO).
The bearer token from the SPA is used only to verify the caller's
identity (JWT validation) — it is never exchanged for a downstream
ARM token.

**Why MI instead of OBO?**
- OBO requires `API_CLIENT_SECRET` and multi-resource consent, which are
  fragile in single-tenant research environments.
- MI simplifies deployment — no secrets to rotate.
- Acceptable trade-off: the MI needs broad permissions, but it is scoped
  to the Function App and auditable via Azure Monitor.

---

## §0 Post-Deploy Permissions Checklist (run after every `azd up`)

> **Important**: The Function App's system-assigned MI gets a **new
> object ID on every fresh `azd up`** (because the Function App resource
> is recreated). Previous role assignments do not carry over. Run this
> checklist after each provision.

### Step 1 — Capture the new MI object ID and target resources

```bash
# From the azd env you just provisioned
RG_PLATFORM="rg-prod2"                # whatever your azd env name resolves to
FUNC_APP="func-prod2-fuxqeza73ska4"   # azd outputs.FUNCTION_APP_NAME
SUB="$(az account show --query id -o tsv)"

MI_OID="$(az functionapp identity show -g "$RG_PLATFORM" -n "$FUNC_APP" \
  --query principalId -o tsv)"
echo "MI object id: $MI_OID"

# Workload resource groups (created/managed via the UI — these are
# OUTSIDE the azd platform RG)
WORKLOAD_RGS=(rg-elb-demo rg-elb-demo-acr rg-elb-demo-terminal)

# User-created storage account + ACR (the ones BLAST jobs read/write)
USER_STORAGE="stgelbdemo"             # in rg-elb-demo
USER_STORAGE_RG="rg-elb-demo"
USER_ACR="elbacrdemo"                 # in rg-elb-demo-acr
USER_ACR_RG="rg-elb-demo-acr"
```

### Step 2 — Workload RGs: Contributor + User Access Administrator

Required for: VM/VNet/AKS/ACR/Storage lifecycle, AKS `listClusterUserCredential`
(direct K8s API calls), and runtime sub-role assignments (AcrPull to kubelet, etc.).

```bash
for rg in "${WORKLOAD_RGS[@]}"; do
  for role in "Contributor" "User Access Administrator"; do
    az role assignment create \
      --assignee-object-id "$MI_OID" \
      --assignee-principal-type ServicePrincipal \
      --role "$role" \
      --scope "/subscriptions/$SUB/resourceGroups/$rg"
  done
done
```

### Step 3 — User storage account: Blob Data Contributor + Blob Delegator

Required for: uploading queries, copying BLAST DBs from NCBI, generating
**User Delegation SAS** (the deployment policy forces `allowSharedKeyAccess=false`,
so account-key SAS is unavailable — delegation SAS is the only option).

```bash
STG_ID="/subscriptions/$SUB/resourceGroups/$USER_STORAGE_RG/providers/Microsoft.Storage/storageAccounts/$USER_STORAGE"
for role in "Storage Blob Data Contributor" "Storage Blob Delegator"; do
  az role assignment create \
    --assignee-object-id "$MI_OID" \
    --assignee-principal-type ServicePrincipal \
    --role "$role" --scope "$STG_ID"
done
```

### Step 4 — User ACR: AcrPush

Required for: `az acr build` REST calls from the `build_acr_images` orchestrator.

```bash
ACR_ID="/subscriptions/$SUB/resourceGroups/$USER_ACR_RG/providers/Microsoft.ContainerRegistry/registries/$USER_ACR"
az role assignment create \
  --assignee-object-id "$MI_OID" \
  --assignee-principal-type ServicePrincipal \
  --role "AcrPush" --scope "$ACR_ID"
```

### Step 5 — Wait for propagation, then verify

Azure RBAC propagation typically takes **1–5 minutes**. The first
1–2 dashboard refreshes after running this script will almost always
show 403s from AKS / Storage / ACR cards. This is **normal**, not a
missing role.

Verify with:

```bash
# Confirm the assignments exist at the expected scopes
az role assignment list --assignee "$MI_OID" \
  --query "[].{role:roleDefinitionName,scope:scope}" -o table
```

If a card is still 403 after 5 minutes, the workload resource is in a
**different RG** (e.g. switching the UI to a cluster in `rg-elb-koc` or
`rg-elb-0509`) — add that RG to `WORKLOAD_RGS` and re-run Step 2.

---

## §1 Function App Managed Identity — Required RBAC Roles

The Function App's **system-assigned Managed Identity** is the principal
that performs all Azure operations. It must be granted the following roles.

### Subscription / Resource Group (ARM Management Plane)

| Role | Scope | Purpose |
|------|-------|---------|
| **Contributor** | Subscription or workload RGs | Create/delete VMs, VNets, AKS, ACR, Storage accounts |
| **User Access Administrator** | Subscription or workload RGs | Assign RBAC roles at runtime (AcrPull to kubelet, Storage roles to VM, etc.) |

> **Least-privilege alternative**: Instead of Subscription-level, grant
> Contributor + User Access Administrator on each resource group:
> `rg-elb`, `rg-elbacr`, `rg-elb-terminal`, `rg-elb-prod`.

### Data Plane — Deployed by Bicep (automatic)

These are assigned by `infra/modules/platform.bicep` during `azd up`:

| Role | Scope | Purpose |
|------|-------|---------|
| Key Vault Secrets Officer | Platform Key Vault | Store/read VM passwords |
| Storage Blob Data Owner | Platform Storage Account | Durable Functions state |
| Storage Queue Data Contributor | Platform Storage Account | Durable Functions queues |
| Storage Table Data Contributor | Platform Storage Account | Durable Functions tables |

### Data Plane — User-Created Resources (manual or runtime)

These must be assigned on user-created storage accounts, ACRs, etc.:

| Role | Scope | Purpose |
|------|-------|---------|
| **Storage Blob Data Contributor** | User storage account (e.g. `stgelbdemo`) | Upload queries, copy DBs from NCBI, list blobs |
| **Storage Blob Delegator** | User storage account | Generate SAS download URLs for results |
| **AcrPush** | User ACR | Build ElasticBLAST images via ACR Build Tasks |
| **Azure Kubernetes Service Cluster User Role** | AKS clusters | Get kubeconfig, run commands |

> The control plane attempts to assign these at runtime via `_assign_role()`.
> If the MI lacks `User Access Administrator`, the assignment soft-fails
> and logs a one-line `az role assignment create` recovery command.

### Quick Setup — MI Role Assignment Commands

See **§0 Post-Deploy Permissions Checklist** above for the full,
copy-pasteable script (with workload RGs, storage, and ACR all covered).

---

## §2 Runtime Role Assignments (Best-Effort)

The Function App MI performs RBAC assignments for other principals at
runtime. All are idempotent and soft-fail if the MI lacks
`Microsoft.Authorization/roleAssignments/write`.

### AKS Kubelet Identity

| Role | Scope | Purpose |
|------|-------|---------|
| AcrPull | ACR | Pull ElasticBLAST container images |
| Storage Blob Data Contributor | User storage account | Download BLAST DB shards to nodes |

### Remote Terminal VM Managed Identity

The Remote Terminal VM is created with a system-assigned Managed Identity.
Cloud-init configures Azure CLI and azcopy to use that identity by default;
`az login --use-device-code` is only for intentionally switching to a personal
Azure CLI session.

| Role | Scope | Purpose |
|------|-------|---------|
| Storage Blob Data Contributor | User storage account | `azcopy` for DB downloads |
| AcrPull | ACR | Pull images if needed |
| Contributor | Workload resource group | `az aks get-credentials`, `kubectl` |

### OpenAPI / Submit Workload Identity

| Role | Scope | Purpose |
|------|-------|---------|
| Contributor | Workload resource group | Run `elastic-blast submit`, including AKS cluster create/read/update operations |
| Storage Blob Data Contributor | User storage account | Upload query/config files and read BLAST DB blobs |
| Azure Kubernetes Service Cluster User Role | AKS cluster | Run Kubernetes API operations from the submit helper job |

### Signed-In User (convenience)

| Role | Scope | Purpose |
|------|-------|---------|
| Storage Blob Data Contributor | User storage account | Direct blob access |
| AcrPush | ACR | Trigger ACR builds |
| Key Vault Secrets Officer | Key Vault | Access VM passwords |

> If any assignment fails, the UI shows a toast with the manual
> `az role assignment create` command.

---

## §3 Signed-In User — Required Roles

The signed-in user needs minimal roles since all Azure operations go
through the Function App MI:

| Role | Scope | Purpose |
|------|-------|---------|
| **Reader** | Subscription | See resources in the UI (cosmetic) |

All data-plane and mutation operations are performed by the MI.

---

## §4 Detailed Role Matrix by Feature

### Virtual Machines (Remote Terminal)

| Feature | MI Role | Scope |
|---|---|---|
| Create/Start/Stop/Delete VM | Contributor | Resource Group |
| Run command on VM | Contributor | VM |

### Networking

| Feature | MI Role | Scope |
|---|---|---|
| Create VNet/Subnet/NSG/NIC/PublicIP | Contributor | Resource Group |

### Storage Account

| Feature | MI Role | Scope |
|---|---|---|
| Create/toggle public access | Contributor | Resource Group / Storage Account |
| List/upload/copy blobs | Storage Blob Data Contributor | Storage Account |
| Generate SAS URL | Storage Blob Delegator | Storage Account |

### Azure Container Registry

| Feature | MI Role | Scope |
|---|---|---|
| Create registry | Contributor | Resource Group |
| Schedule ACR build | AcrPush or Contributor | Registry |

### Azure Kubernetes Service

| Feature | MI Role | Scope |
|---|---|---|
| Create/delete/start/stop cluster | Contributor | Resource Group |
| Get kubeconfig, run command | AKS Cluster User Role | Cluster |
| Direct K8s API (pods, jobs, metrics) | AKS Cluster User Role | Cluster |

### Key Vault

| Feature | MI Role | Scope |
|---|---|---|
| Create/update vault | Contributor | Resource Group |
| Store/read/delete secrets | Key Vault Secrets Officer | Vault |

---

## §5 Troubleshooting

| Error | Cause | Fix |
|---|---|---|
| `AuthorizationFailed` on ARM operations | MI lacks **Contributor** | Assign Contributor on the target RG or subscription |
| `AuthorizationPermissionMismatch` on blobs | MI lacks **Storage Blob Data Contributor** | Assign data-plane role on the storage account |
| `ForbiddenByRbac` on Key Vault | MI lacks **Key Vault Secrets Officer** | Assign on the vault |
| `does not have authorization` on RBAC | MI lacks **User Access Administrator** | Assign at target scope; or run the logged `az` command manually |
| `Forbidden` on AKS kubeconfig | MI lacks **AKS Cluster User Role** | Assign on the cluster |
| RBAC assigned but still failing | Propagation delay (typically 1–5 min; observed 403→200 within ~70s on `listClusterUserCredential`) | Wait and retry; verify with `az role assignment list --assignee <MI_OID>` |
| `No identity found` | MI not enabled | Portal → Function App → Identity → System-assigned → On |

---

## §6 Security Notes

- The MI has broad permissions by design — acceptable for a single-tenant
  research deployment where the MI is scoped to one Function App.
- `api/auth/obo.py` exists but is **dead code** (never imported). All
  auth goes through MI via `DefaultAzureCredential`.
- The bearer token is validated but never used for downstream calls.
- NSG on the Remote Terminal restricts SSH to the caller's IP.
- Storage `publicNetworkAccess` is `Enabled` (Y1 plan limitation) but
  secured via RBAC — no anonymous access.
