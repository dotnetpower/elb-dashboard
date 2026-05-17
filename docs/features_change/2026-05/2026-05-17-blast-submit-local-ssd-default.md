# BLAST Submit Local SSD Default

## Motivation

API submit smoke testing showed that a default BLAST submission could fall back
to ElasticBLAST's shared PV/PVC database initialization path. That path requested
a `blast-dbs-pvc-rwm` claim backed by `azureblob-nfs-premium`, but the dashboard
runtime is designed around AKS node-local SSD warmup/cache usage instead of a
shared PVC dependency.

## User-Facing Change

Dashboard and API submissions now default to ElasticBLAST's node-local SSD init
path by setting `cluster.exp-use-local-ssd = true` unless a caller explicitly
sets `use_local_ssd = false` for baseline debugging. Automatic shard
partitioning remains a separate mode; local SSD usage no longer depends on
sharding being enabled.

## API / Config Diff Summary

- `api.services.blast_config.generate_config` enables local SSD by default.
- `/api/blast/*` submit option filtering accepts `use_local_ssd`.
- The React submit payload sends `use_local_ssd: true` explicitly.
- Tests now assert that default config avoids `db-partitions` while still using
  local SSD.

## Validation Evidence

- `uv run pytest -q api/tests/test_blast_config_sharding.py api/tests/test_blast_tasks.py`
- `cd web && npm run build`