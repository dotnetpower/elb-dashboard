# Force Local SSD for BLAST Submit

## Motivation

The shared PV/PVC ElasticBLAST path is intentionally paused until it is productized and stabilized in this dashboard. For now, every browser/API BLAST submit should use the AKS node-local SSD path.

## User-Facing Change

- BLAST submit always uses local SSD initialization, even if an older client or saved draft sends `use_local_ssd=false`.
- The submitted job payload is normalized to `use_local_ssd=true` so job details and reruns reflect the active runtime policy.

## API / IaC Diff Summary

- `api.services.blast_config.generate_config` always writes `cluster.exp-use-local-ssd=true`.
- `api.routes.stubs._submit_options_from_body` and `_normalise_blast_submit_body` force `use_local_ssd=true` at the HTTP boundary.
- PV/PVC code remains in the repository for future work, but no active BLAST submit path should select it.
- No IaC changes.

## Validation Evidence

- `uv run ruff format api/services/blast_config.py api/routes/stubs.py api/tests/test_blast_config_sharding.py api/tests/test_blast_submit_route_options.py` — passed, 4 files unchanged.
- `PYTHONPATH=$PWD uv run pytest -q api/tests/test_blast_config_sharding.py api/tests/test_blast_submit_route_options.py api/tests/test_blast_tasks.py` — passed, 119 tests.
- `uv run ruff check api/services/blast_config.py api/routes/stubs.py api/tests/test_blast_config_sharding.py api/tests/test_blast_submit_route_options.py` — passed.
- `python3 -m py_compile api/services/blast_config.py api/routes/stubs.py api/tests/test_blast_config_sharding.py api/tests/test_blast_submit_route_options.py && git diff --check` — passed.
