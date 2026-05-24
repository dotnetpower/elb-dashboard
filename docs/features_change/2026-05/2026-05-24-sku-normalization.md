# SKU Normalization for Warmup Capacity

## Motivation

The submit page could compute warmup and sharding capacity with the conservative unknown-SKU fallback when the selected AKS node pool SKU arrived as an Azure-style alias such as `E32as_v7` instead of the catalog spelling `Standard_E32as_v7`. That made the UI report a 32 GiB safe budget for a 256 GiB node.

## User-facing change

Warmup advisories and shard-capacity previews now normalize known SKU aliases before looking up node RAM. A 3-node `Standard_E32as_v7` blastpool is evaluated with 256 GiB RAM per node and a 128 GiB safe budget instead of the fallback 64 GiB RAM / 32 GiB safe budget.

## API/IaC diff summary

- Added shared backend SKU normalization in `api.services.aks_skus` and applied it to warmup feasibility and submit sharding calculations.
- Added matching frontend normalization for submit-page capacity previews and selected-cluster topology.
- No IaC changes.

## Validation evidence

- `uv run pytest -q api/tests/test_aks_skus.py api/tests/test_warmup_planner.py api/tests/test_db_sharding.py api/tests/test_blast_databases_warmup_plan.py`
- `cd web && npm run test -- src/utils/dbSharding.test.ts src/pages/blastSubmit/shardingAvailability.test.ts`