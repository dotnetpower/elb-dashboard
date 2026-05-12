# AKS Workload Identity for OpenAPI Service

**Date**: 2026-05-11

## Motivation

The OpenAPI pod (`elb-openapi`) inside AKS ran `az account show` for its health check, which always failed because pods have no interactive `az login` session. This caused `/health` to return `"status": "degraded"` permanently, and `azcopy` operations (FASTA upload, results download) also failed.

## User-facing Change

- `/health` now returns `"status": "healthy"` with `"method": "DefaultAzureCredential"`
- All 9 OpenAPI endpoints work: healthz, health, config, cluster, jobs CRUD, results
- Job submission (POST /jobs) successfully uploads queries via azcopy with Workload Identity
- New workspaces automatically get full Workload Identity setup via AKS provisioning orchestrator

## Technical Changes

### Control Plane (`elastic-blast-azure-functionapp`)

| File | Change |
|------|--------|
| `api/function_app.py` | AKS creation: added `oidc_issuer_profile.enabled`, `security_profile.workload_identity.enabled` |
| `api/function_app.py` | New `setup_workload_identity_activity`: creates User-Assigned MI, Federated Credential, assigns Storage Blob Data Contributor + AKS Cluster User roles |
| `api/function_app.py` | New `deploy_openapi_activity`: creates K8s ServiceAccount (WI-annotated), ClusterRole, ClusterRoleBinding, Deployment, Service |
| `api/orchestrators/provision_aks.py` | Extended from 2 steps to 4: create cluster → assign kubelet roles → setup WI → deploy OpenAPI |
| `api/requirements.txt` | Added `azure-mgmt-msi==7.0.0` |

### OpenAPI Service (`elastic-blast-azure`)

| File | Change |
|------|--------|
| `docker-openapi/app/main.py` | Health check: `az account show` → `DefaultAzureCredential().get_token()` |
| `docker-openapi/app/main.py` | Startup bootstrap: `_wi_az_login()` — federated token → `az login` at pod start |
| `docker-openapi/app/main.py` | Version bump: 3.1.0 → 3.2.0 |
| `docker-openapi/app/requirements.txt` | Added `azure-identity>=1.17.0` |

### K8s Resources (created by deploy_openapi_activity)

- `ServiceAccount/elb-openapi-sa` with `azure.workload.identity/client-id` annotation
- `ClusterRole/elb-openapi-role` — nodes, pods, configmaps, services, batch/jobs access
- `ClusterRoleBinding/elb-openapi-binding`
- `Deployment/elb-openapi` — WI label, AZCOPY_AUTO_LOGIN_TYPE=AZCLI, AZURE_CLIENT_ID injected
- `Service/elb-openapi` — LoadBalancer on port 80 → 8000

## Validation

- `GET /healthz` → 200 `{"status": "ok"}`
- `GET /health` → 200 `{"status": "healthy", "checks": {"kubernetes": {"status": "ok", "nodes": 3}, "azure_auth": {"status": "ok", "method": "DefaultAzureCredential"}}}`
- `POST /jobs` (Mode B inline FASTA) → 202, azcopy upload succeeded
- `DELETE /jobs/{id}` → 200, ConfigMap cleaned
- Pod logs confirm: `az login succeeded via Workload Identity (client=6ca73d96)`
