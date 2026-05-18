# Workload Storage Private Endpoints for Container Apps

## Motivation

Workload Storage accounts can have `publicNetworkAccess=Disabled`, but the browser control plane runs in the Container Apps platform VNet. A workload account that only has a private endpoint in the AKS VNet is reachable by AKS nodes but not by the api, worker, or terminal sidecars.

## User-facing change

Provisioning a workload Storage account now also ensures `blob` and `dfs` private endpoints in the platform VNet and attaches them to the platform private DNS zones. The Storage account can remain closed to public networks while the deployed control plane can list, upload, download, and warm BLAST data through private networking.

## API / IaC diff summary

- Added `api.services.storage_network.ensure_workload_storage_private_endpoints()` for idempotent blob + dfs private endpoint and DNS zone group creation.
- Extended `/api/resources/ensure-storage` to pass platform private endpoint subnet and DNS zone resource group from server environment variables.
- Added `PLATFORM_PRIVATE_ENDPOINT_SUBNET_ID`, `PLATFORM_PRIVATE_DNS_ZONE_RESOURCE_GROUP`, and `AZURE_RESOURCE_GROUP` to api/worker sidecar environment variables in Bicep.
- Passed `network.outputs.privateEndpointsSubnetId` into the Container App module.

## Validation evidence

- Manually added `pe-elbstg01-blob` and `pe-elbstg01-dfs` in `rg-elb-ca` on `vnet-elb-ca/snet-private-endpoints`.
- Platform private DNS records now resolve `elbstg01.blob.core.windows.net` to `10.20.2.10` and `elbstg01.dfs.core.windows.net` to `10.20.2.11` from the api sidecar.
- Api sidecar data-plane smoke test listed workload containers: `blast-db`, `queries`, `results`.
- ACR stayed locked: `publicNetworkAccess=Disabled`, `defaultAction=Deny`.
- `uv run pytest -q api/tests/test_storage_network.py` -> passed.
- `uv run ruff check api/services/storage_network.py api/services/monitoring.py api/routes/resources.py api/tests/test_storage_network.py` -> passed.
- `az bicep build --file infra/main.bicep --stdout` -> passed.
- Deployed api/worker/beat image `acrelbnm5virmqrdi5c.azurecr.io/elb-api:20260518155822`.
- Container App revision `ca-elb-control--0000057` is `Healthy` with 100% traffic.
- Public `/api/health` -> `200`, revision `ca-elb-control--0000057`.
