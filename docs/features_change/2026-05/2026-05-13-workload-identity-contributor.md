# Workload Identity Contributor Role

## Motivation

BLAST submit failed in production when the AKS-hosted submit helper ran `elastic-blast submit` with the OpenAPI workload identity. The identity had storage and AKS cluster-user roles, but it did not have permission to create or read AKS managed clusters in the workload resource group.

## User-facing change

Re-running the AKS/OpenAPI provisioning flow now grants the OpenAPI workload identity `Contributor` on the workload resource group, so submit jobs can create or reuse the ElasticBLAST AKS cluster without `AuthorizationFailed` errors on `Microsoft.ContainerService/managedClusters/read` or `write`.

## API/IaC diff summary

- Updated `setup_workload_identity_activity` to assign `Contributor` to the OpenAPI user-assigned managed identity at the workload resource group scope.
- The activity response now includes a `roles_assigned` list for the workload identity.
- RBAC assignment reporting is now truthful: failed best-effort assignments are returned in `roles_failed` instead of being listed as assigned.
- Submit helper jobs and the OpenAPI deployment now set `PATH=/opt/venv/bin:...` so Azure CLI is found in images that install it in the isolated CLI virtualenv.
- Submit helper jobs now preserve `az login` stderr in pod logs and removed stale unused WebSocket exec scaffolding.
- Documented the OpenAPI / submit workload identity RBAC requirements in `docs/auth.md`.
- No IaC changes.

## Validation evidence

- Production hotfix: granted `Contributor` on `rg-elb-demo` to UAMI `id-elb-openapi` (`principalId` `4204614f-5056-414e-b0ac-15da55352be1`).
- Python syntax check and Function App import check passed for `api/services/network.py`, `api/activities/blast.py`, `api/function_app.py`, and `api/routes/aks.py`.
- `git diff --check` passed for the hardening files.
- Production smoke discovered the deployed `elb-openapi:3.4` image has `az` at `/opt/venv/bin/az` but not on PATH; code now sets PATH explicitly before `az login`.
- Existing OpenAPI pod smoke passed with explicit PATH: Workload Identity `az login` succeeded and `az aks show -g rg-elb-demo -n elb-cluster-01 --query provisioningState -o tsv` returned `Succeeded`.
- `scripts/dev/deploy-api.sh` deployed `funcapp-202605140027.zip` and reported `/api/health` HTTP 200.