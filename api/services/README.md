# api/services/

Single source of truth for Azure SDK calls and shared domain logic.
Routes and Celery tasks import from here — **never `azure.mgmt.*` or
`azure.identity` directly** outside this package.

For the responsibility table per file see
[docs/copilot/codebase-map.md §2](../../docs/copilot/codebase-map.md#2-backend-services-apiservices).

## Boundaries (load-bearing)

| Rule | Why |
|------|-----|
| Only [azure_clients.py](./azure_clients.py) (and `keyvault.py` for its KV client) imports `azure.mgmt.*`. | Single chokepoint for credential + retry policy. |
| [storage_data.py](./storage_data.py) **must not** import `generate_blob_sas`, `get_user_delegation_key`, or `BlobSasPermissions`. | Browser must never receive a SAS token (charter §9). Load-bearing comment lives at the bottom of the file. |
| [monitoring.py](./monitoring.py) **must not** call `ManagedClusters.begin_run_command` / `VirtualMachines.begin_run_command`. | ~30 s slow + ARM-rate-limited. Use `k8s_*` helpers or [terminal_exec.py](./terminal_exec.py). |
| [terminal_exec.py](./terminal_exec.py) is the only path to shell tooling (`azcopy`, `kubectl`, `elastic-blast`, `elb`, `az`). | argv[0] allowlisted server-side in [terminal/exec_server.py](../../terminal/exec_server.py). api/worker images intentionally do NOT ship those CLIs. |
| [storage_public_access.py](./storage_public_access.py) keeps the `CONTAINER_APP_NAME` guard. | Deployed Container Apps must never flip Storage `publicNetworkAccess` open. |

## Groupings

* **Azure SDK boundary**: `azure_clients`, `keyvault`, `storage_data`, `storage_network`, `storage_public_access`, `network`, `passwords`.
* **BLAST domain**: `blast/`, `blast_config`, `blast_db_metadata`, `blast_oracles`, `blast_results_parser`, `db_order_oracle`, `db_sharding`, `external_blast`, `query_grouping`, `query_metadata`, `sharding_precision`, `warmup/`, `warmup_jobs`, `warmup_planner`, `auto_warmup`, `auto_warmup_reconcile`, `web_blast_searchsp`, `taxonomy`, `taxonomy_image`.
* **Monitoring + state**: `monitoring`, `k8s/`, `k8s_monitoring`, `monitor_cache`, `state_repo`, `request_metrics`, `sidecar_metrics`, `event_emitter`, `aks_skus`, `image_tags`, `openapi_runtime`, `cgroup_reporter`.
* **Terminal exec channel**: `terminal_exec`, `sanitise`.

## Adding a new service

1. Decide the grouping (above). One file = one cohesive responsibility.
2. If it talks to Azure, route the credential through
   [azure_clients.py](./azure_clients.py) (`_get_mi_credential` + the
   `*_client` factories).
3. Add a one-line entry in [docs/copilot/codebase-map.md §2](../../docs/copilot/codebase-map.md#2-backend-services-apiservices)
   and (if its scope justifies) the grouping list above.
4. Tests live in [api/tests/](../tests/) — same-name pattern (`test_<service>.py`).
