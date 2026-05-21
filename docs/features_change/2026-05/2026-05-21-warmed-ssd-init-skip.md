# Warmed SSD Init Skip

## Motivation

Dashboard-managed sharding and node warmup can already prove that every local-SSD shard cache is ready before submit. The sibling ElasticBLAST runtime still recreated and waited on `init-ssd-*` setup Jobs, making submit appear stuck even when the warmed cache was complete.

## User-facing change

Sharded BLAST submits that pass the dashboard warmup readiness gate now write an opt-in ElasticBLAST config flag so the sibling runtime can skip redundant local-SSD DB initialization. If the sibling runtime cannot verify the dashboard warmup Jobs, it falls back to the existing setup Job path.

## API/IaC diff summary

- Add `[cluster] exp-skip-warmed-ssd-init = true` to generated configs only when the submit warmup gate returned Ready.
- No API route or IaC changes.
- Requires the sibling `elastic-blast-azure` change that verifies `app=elb-db-warmup` Jobs before skipping `init-ssd-*`.

## Validation evidence

- `PYTHONPATH=src pytest -q tests/azure/test_db_partitioning.py::TestPartitionConfig::test_cluster_skip_warmed_ssd_init_default tests/azure/test_db_partitioning.py::TestInitializeLocalSsdSharded::test_skips_init_jobs_when_dashboard_warmup_ready` in sibling repo: 2 passed.
- `PYTHONPATH=src pytest -q tests/azure/test_db_partitioning.py` in sibling repo: 40 passed.
- `uv run pytest -q api/tests/test_blast_config_sharding.py api/tests/test_blast_tasks.py`: 138 passed.
- `uv run ruff check api/services/blast_config.py api/tasks/blast/__init__.py api/tests/test_blast_config_sharding.py`: passed.
