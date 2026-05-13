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

```bash
MI_OID="<function-app-mi-object-id>"  # Portal → Function App → Identity
SUB="/subscriptions/<subscription-id>"

# Subscription-level (simplest)
az role assignment create --assignee-object-id "$MI_OID" \
  --assignee-principal-type ServicePrincipal \
  --role Contributor --scope "$SUB"

az role assignment create --assignee-object-id "$MI_OID" \
  --assignee-principal-type ServicePrincipal \
  --role "User Access Administrator" --scope "$SUB"

# User data storage
STORAGE_ID="$SUB/resourceGroups/<rg>/providers/Microsoft.Storage/storageAccounts/<name>"
az role assignment create --assignee-object-id "$MI_OID" \
  --assignee-principal-type ServicePrincipal \
  --role "Storage Blob Data Contributor" --scope "$STORAGE_ID"

az role assignment create --assignee-object-id "$MI_OID" \
  --assignee-principal-type ServicePrincipal \
  --role "Storage Blob Delegator" --scope "$STORAGE_ID"
```

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
| RBAC assigned but still failing | Propagation delay (up to 5 min) | Wait and retry |
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
