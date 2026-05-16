# New Search Workload Node Pool

## Motivation

New Search displayed the system AKS node pool as the compute environment because the AKS monitor summary used the first agent pool. ElasticBLAST jobs run on the user workload pool, normally `blastpool`, so the form could show and submit the wrong SKU and node count.

## User-facing change

The Compute Environment section now displays the workload node pool, preferring `blastpool`, then any `mode=User` pool. The submit payload uses the same workload pool for `machine_type` and `num_nodes`.

## API/IaC diff summary

- `api.services.monitoring.list_aks_clusters` now summarizes AKS `node_sku` and `node_count` from the workload pool while preserving the full `agent_pools` list.
- New Search frontend derives its displayed pool, SKU, node count, sharding preview, and submit payload from the workload pool.
- No IaC changes.

## Validation evidence

- Added `api/tests/test_monitoring_aks_pools.py` for `blastpool` and generic user-pool fallback selection.
- `uv run pytest -q api/tests/test_monitoring_aks_pools.py` -> 2 passed.
- `uv run pytest -q api/tests` -> 265 passed.
- `cd web && npm run build` -> built successfully.