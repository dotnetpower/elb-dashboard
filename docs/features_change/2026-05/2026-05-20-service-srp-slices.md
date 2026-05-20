# Service SRP Slices

## Motivation
Several backend task/service modules had accumulated unrelated responsibilities after the route package split and auto-warmup hardening. The next cleanup step is to keep Celery task files focused on orchestration while moving reusable domain policy into `api/services/`.

## User-facing change
No intended UI or API behavior change. Existing task names, private compatibility helpers used by tests, and route behavior remain stable.

## API/IaC diff summary
- Added `api/services/k8s/client.py` for AKS kubeconfig credential caching and Kubernetes session setup.
- Added `api/services/k8s/nodes.py` for node projection and Ready warmup-node selection.
- Kept `api.services.k8s_monitoring` as the compatibility facade for existing public and private import surfaces.
- Added `api/services/warmup/task_planning.py` for Storage warmup shard, molecule type, and image planning helpers.
- Added `api/services/blast/task_config.py` for BLAST task URL/path normalization, ElasticBLAST config generation, and node-warmup submit readiness policy.
- Converted Celery task entry modules to package directories (`api/tasks/acr/`, `api/tasks/azure/`, `api/tasks/blast/`, `api/tasks/openapi/`, `api/tasks/storage/`) while preserving `api.tasks.<name>` import paths and explicit Celery task names.
- Kept flat service compatibility wrappers (`k8s_client.py`, `k8s_nodes.py`, `blast_task_config.py`, `warmup_task_planning.py`) so existing imports and tests keep working during the broader SRP migration.
- Added explicit `__all__` lists to those compatibility wrappers so lint/auto-format passes cannot accidentally remove the compatibility surface.
- Updated service maps in `api/services/README.md` and `docs/copilot/codebase-map.md`.
- No IaC changes.

## Validation evidence
- `PYTHONPATH=$PWD uv run pytest -q api/tests/test_k8s_list_events.py api/tests/test_k8s_release_stale_warmup_jobs.py api/tests/test_warmup_jobs.py::test_candidate_warmup_nodes_prefers_blastpool_ready_nodes api/tests/test_warmup_jobs.py::test_ensure_job_manifests_is_idempotent_for_existing_jobs` → 12 passed.
- `PYTHONPATH=$PWD uv run pytest -q api/tests/test_warmup_jobs.py::test_select_warmup_shards_uses_feasible_ten_way_core_nt api/tests/test_auto_warmup.py::test_warmup_database_auto_strict_waits_for_requested_ready_nodes` → 2 passed.
- `PYTHONPATH=$PWD uv run pytest -q api/tests/test_blast_tasks.py::test_build_config_content_targets_existing_cluster_and_storage_urls api/tests/test_blast_tasks.py::test_build_config_approximate_sharding_opt_in_injects_partitions api/tests/test_blast_tasks.py::test_node_warmup_ready_check_allows_ready_sharded_submit api/tests/test_blast_tasks.py::test_node_warmup_ready_check_skips_stale_sharded_options_for_unsharded_db api/tests/test_blast_tasks.py::test_query_blob_path_from_query_file_accepts_queries_paths api/tests/test_blast_tasks.py::test_query_blob_path_from_query_file_rejects_unsafe_inputs` → 12 passed.
- `uv run ruff check api` + focused package compatibility tests (`test_k8s_list_events.py`, `test_k8s_release_stale_warmup_jobs.py`, selected BLAST/warmup tests) → passed.
