# Authentication & Required Permissions

This document lists every Azure RBAC role the signed-in user needs to
operate the ElasticBLAST control plane. All Azure mutations run under the
user's own identity via **On-Behalf-Of (OBO)** — no service-principal
secrets are stored.

---

## Quick Start — Minimum Viable Setup

If you want a single command that covers everything:

```bash
# Owner at subscription level (covers ARM management + RBAC assignment)
az role assignment create \
  --assignee <user-email-or-oid> \
  --role Owner \
  --scope /subscriptions/<subscription-id>

# Data-plane roles must be assigned separately (Owner alone is NOT enough)
az role assignment create \
  --assignee <user-email-or-oid> \
  --role "Storage Blob Data Contributor" \
  --scope /subscriptions/<subscription-id>

az role assignment create \
  --assignee <user-email-or-oid> \
  --role "Storage Blob Delegator" \
  --scope /subscriptions/<subscription-id>

az role assignment create \
  --assignee <user-email-or-oid> \
  --role "Key Vault Secrets Officer" \
  --scope /subscriptions/<subscription-id>
```

> **Note**: RBAC propagation can take up to 5 minutes after assignment.

---

## Detailed Role Matrix

### 1. Subscription / Resource Group (ARM Management Plane)

| Feature | Azure Operation | Minimum Role | Scope |
|---|---|---|---|
| List subscriptions | `SubscriptionClient.subscriptions.list()` | **Reader** | Tenant |
| List resource groups | `resource_groups.list()` | **Reader** | Subscription |
| Create resource group | `resource_groups.create_or_update()` | **Contributor** | Subscription |
| Delete resource group | `resource_groups.begin_delete()` | **Contributor** | Resource Group |
| Set resource group tags | `resource_groups.create_or_update()` (tag merge) | **Contributor** | Resource Group |
| Assign RBAC roles | `role_assignments.create()` | **User Access Administrator** | Target scope |

### 2. Virtual Machines (Remote Terminal)

| Feature | Azure Operation | Minimum Role | Scope |
|---|---|---|---|
| List VMs | `virtual_machines.list()` | **Reader** | Resource Group |
| Get VM status | `virtual_machines.get(expand=instanceView)` | **Reader** | Resource Group |
| Create VM | `virtual_machines.begin_create_or_update()` | **Virtual Machine Contributor** | Resource Group |
| Start VM | `virtual_machines.begin_start()` | **Virtual Machine Contributor** | Resource Group |
| Stop (deallocate) VM | `virtual_machines.begin_deallocate()` | **Virtual Machine Contributor** | Resource Group |
| Delete VM | `virtual_machines.begin_delete()` | **Virtual Machine Contributor** | Resource Group |
| Run command on VM | `virtual_machines.begin_run_command()` | **Virtual Machine Contributor** | Resource Group |

### 3. Networking

| Feature | Azure Operation | Minimum Role | Scope |
|---|---|---|---|
| Create VNet / Subnet / NSG / NIC / Public IP | `begin_create_or_update()` | **Network Contributor** | Resource Group |
| Add SSH rule to NSG | `security_rules.begin_create_or_update()` | **Network Contributor** | Resource Group |
| Delete NIC / Public IP / NSG | `begin_delete()` | **Network Contributor** | Resource Group |
| Read NIC / Public IP | `.get()` | **Reader** | Resource Group |

### 4. Storage Account (Management Plane)

| Feature | Azure Operation | Minimum Role | Scope |
|---|---|---|---|
| List storage accounts | `storage_accounts.list_by_resource_group()` | **Reader** | Resource Group |
| Get storage properties | `storage_accounts.get_properties()` | **Reader** | Resource Group |
| Create storage account | `storage_accounts.begin_create()` | **Storage Account Contributor** | Resource Group |
| Toggle public network access | `storage_accounts.update()` | **Storage Account Contributor** | Storage Account |
| Create blob container | `blob_containers.create()` | **Storage Account Contributor** | Storage Account |

### 5. Storage Account (Data Plane) ⚠️

> **These are data-plane roles and must be assigned explicitly.** ARM-level
> Owner/Contributor does NOT grant data-plane access.

| Feature | Azure Operation | Minimum Role | Scope |
|---|---|---|---|
| List BLAST databases | `list_blobs()` | **Storage Blob Data Reader** | Storage Account |
| Upload query FASTA | `upload_blob()` | **Storage Blob Data Contributor** | Storage Account |
| Download/read blob | `download_blob()` | **Storage Blob Data Reader** | Storage Account |
| Copy DB from NCBI S3 | `start_copy_from_url()` | **Storage Blob Data Contributor** | Storage Account |
| Generate SAS download URL | `get_user_delegation_key()` | **Storage Blob Delegator** | Storage Account |

**Recommended**: Assign **Storage Blob Data Contributor** + **Storage Blob Delegator**
at the storage account scope (covers all of the above).

### 6. Azure Container Registry (ACR)

| Feature | Azure Operation | Minimum Role | Scope |
|---|---|---|---|
| List registries | `registries.list_by_resource_group()` | **Reader** | Resource Group |
| Get registry info | `registries.get()` | **Reader** | Registry |
| List build runs | `runs.list()` | **Reader** | Registry |
| Create registry | `registries.begin_create()` | **Contributor** | Resource Group |
| Schedule ACR build | `registries.begin_schedule_run()` | **Contributor** | Registry |

### 7. Azure Kubernetes Service (AKS)

**ARM roles**:

| Feature | Azure Operation | Minimum Role | Scope |
|---|---|---|---|
| List clusters | `managed_clusters.list_by_resource_group()` | **Reader** | Resource Group |
| Get cluster | `managed_clusters.get()` | **Reader** | Resource Group |
| Create cluster | `managed_clusters.begin_create_or_update()` | **Contributor** | Resource Group |
| Delete cluster | `managed_clusters.begin_delete()` | **Contributor** | Resource Group |
| Start / stop cluster | `begin_start()` / `begin_stop()` | **Contributor** | Cluster |
| Get kubeconfig | `list_cluster_user_credentials()` | **Azure Kubernetes Service Cluster User Role** | Cluster |
| Run AKS command | `begin_run_command()` | **Azure Kubernetes Service Cluster User Role** | Cluster |

**Kubernetes RBAC** (inside the cluster):

| Feature | K8s API | Minimum K8s ClusterRole |
|---|---|---|
| List nodes, pods | `GET /api/v1/nodes`, `/pods` | `view` |
| List jobs | `GET /apis/batch/v1/namespaces/{ns}/jobs` | `view` |
| Get pod logs | `GET /api/v1/.../pods/{pod}/log` | `view` |
| Get node metrics | `GET /apis/metrics.k8s.io/v1beta1/nodes` | `view` + metrics-server |

> If AKS uses AAD-integrated RBAC, also assign
> **Azure Kubernetes Service RBAC Reader** at the cluster scope.

### 8. Key Vault ⚠️

> **Data-plane roles must be assigned explicitly** (same as Storage).

**Management plane**:

| Feature | Azure Operation | Minimum Role | Scope |
|---|---|---|---|
| Get vault config | `vaults.get()` | **Reader** | Resource Group |
| Create / update vault | `vaults.begin_create_or_update()` | **Contributor** | Resource Group |

**Data plane**:

| Feature | Azure Operation | Minimum Role | Scope |
|---|---|---|---|
| Store VM password | `set_secret()` | **Key Vault Secrets Officer** | Vault |
| Reveal VM password | `get_secret()` | **Key Vault Secrets User** | Vault |
| Delete secret (teardown) | `begin_delete_secret()` | **Key Vault Secrets Officer** | Vault |

---

## Summary — Roles by Scope

| Scope | Role | Purpose |
|---|---|---|
| **Subscription** | Contributor | Create/delete resource groups & resources |
| **Subscription** | User Access Administrator | Assign RBAC roles (e.g. AcrPull to AKS kubelet) |
| **Storage Account** | Storage Blob Data Contributor | Upload queries, copy DBs, list blobs |
| **Storage Account** | Storage Blob Delegator | Generate SAS download URLs |
| **Key Vault** | Key Vault Secrets Officer | Store/delete VM passwords |
| **AKS Cluster** | Azure Kubernetes Service Cluster User Role | Get kubeconfig, run commands |

> **Least-privilege alternative to Subscription Contributor**: assign
> Contributor only on the specific resource groups used by ElasticBLAST
> (e.g. `rg-elb`, `rg-elbacr`, `rg-elb-terminal`).

---

## Assignment Commands (per resource)

```bash
USER="<user-email-or-object-id>"
SUB="<subscription-id>"

# --- Subscription-level ---
az role assignment create --assignee "$USER" --role Contributor \
  --scope "/subscriptions/$SUB"
az role assignment create --assignee "$USER" --role "User Access Administrator" \
  --scope "/subscriptions/$SUB"

# --- Storage Account data plane ---
STORAGE_ID="/subscriptions/$SUB/resourceGroups/<rg>/providers/Microsoft.Storage/storageAccounts/<name>"
az role assignment create --assignee "$USER" --role "Storage Blob Data Contributor" \
  --scope "$STORAGE_ID"
az role assignment create --assignee "$USER" --role "Storage Blob Delegator" \
  --scope "$STORAGE_ID"

# --- Key Vault data plane ---
KV_ID="/subscriptions/$SUB/resourceGroups/<rg>/providers/Microsoft.KeyVault/vaults/<name>"
az role assignment create --assignee "$USER" --role "Key Vault Secrets Officer" \
  --scope "$KV_ID"

# --- AKS cluster ---
AKS_ID="/subscriptions/$SUB/resourceGroups/<rg>/providers/Microsoft.ContainerService/managedClusters/<name>"
az role assignment create --assignee "$USER" \
  --role "Azure Kubernetes Service Cluster User Role" \
  --scope "$AKS_ID"
```

---

## Troubleshooting

| Error Message | Cause | Fix |
|---|---|---|
| `AuthorizationFailure` on blob operations | Missing **Storage Blob Data Reader/Contributor** | Assign data-plane role on the storage account |
| `AuthorizationPermissionMismatch` | Wrong scope (e.g. RG-level instead of account-level) | Re-assign at correct scope |
| `ForbiddenByRbac` on Key Vault | Missing **Key Vault Secrets User/Officer** | Assign data-plane role on the vault |
| `does not have authorization to perform action` on RBAC | Missing **User Access Administrator** | Assign at the target resource scope |
| `Forbidden` on AKS kubeconfig | Missing **AKS Cluster User Role** | Assign on the cluster |
| RBAC role assigned but still failing | Propagation delay (up to 5 min) | Wait and retry |
