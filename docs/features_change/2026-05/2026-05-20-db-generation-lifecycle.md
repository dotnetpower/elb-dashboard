# BLAST DB Generation Lifecycle

## Motivation

NCBI BLAST database updates are snapshot-like generations, identified by the public `latest-dir` value. The dashboard previously treated a downloaded DB, shard layout, warmup Job, and DB-order oracle as one timeless state. That could make an old shard layout or node-local warm cache look ready after a newer NCBI generation was available.

## User-Facing Change

- Blast Databases now treats updates as an explicit operation. Updating an already downloaded DB opens a confirmation that calls out the full server-side copy, shard rebuild, stale warmup, and stale oracle implications.
- Downloaded DB rows surface `Updating`, `Update failed`, `Shards stale`, and `Order stale` chips.
- Auto warmup is disabled for stale or updating downloaded DB generations and skips stale downloads with `update_required`.
- Cluster database chips surface `warm stale` when node-local warmup Jobs belong to old or mixed DB generations.

## API / Worker Change

- `POST /api/storage/prepare-db` writes update lifecycle metadata before copy initiation and only promotes `source_version` after copy initiation succeeds.
- `GET /api/blast/databases` exposes update and shard-generation fields: `update_in_progress`, `updating_to_source_version`, `update_error`, `shard_source_version`, and `shards_stale`.
- Warmup Jobs are annotated with `elb.dashboard/source-version` and include `ELB_DB_SOURCE_VERSION` in the pod env.
- Warmup status aggregates shard-named jobs under the logical DB and marks mixed source versions as `Stale`.
- Stale warmup release deletes jobs pinned to old nodes or old DB source versions.
- DB-order oracle creation rejects updating DBs, stale shard layouts, stale warmup, and stale client `source_version` payloads.
- Scheduled DB update checks compare downloaded `source_version` values against NCBI `latest-dir`.
- Submit-time sharding eligibility now uses prepared shard metadata and current generation checks instead of a `core_nt` special case, and sharded submits wait when node-local warmup belongs to an older DB generation.

## Critique / Hardening

1. Old generation overwritten too early: fixed by preserving current `source_version` while update copy initiation is in progress.
2. Copy-init failure could poison current DB metadata: fixed by recording `update_error` without promotion.
3. Shard layouts could be reused across generations: fixed with `shard_source_version` and `shards_stale`.
4. Auto warmup could warm a stale download: fixed by comparing downloaded generation with NCBI `latest-dir`.
5. Auto warmup could skip because an old generation was already Ready: fixed by comparing warmup generation with storage generation.
6. Warmup Jobs lacked generation identity: fixed with Job and pod-template annotations plus env propagation.
7. Mixed old/new warmup Jobs could aggregate as Ready: fixed by returning `Stale` for multiple warmup source versions.
8. Legacy shard job labels could pollute the dashboard with per-shard DB names: fixed by aggregating `*_shard_XX` under the logical DB.
9. AKS restart cleanup only handled stale node names: fixed by also deleting stale source-version Jobs.
10. Oracle build could target stale warmup or stale client payloads: fixed with storage metadata and warmup generation validation before Job creation.
11. UI update action looked like a normal download: fixed with an explicit confirmation and update-specific result text.
12. UI could keep showing update-available while update was in progress: fixed by separating `update_in_progress` from stale count/action state.
13. Cluster chips hid warm generation drift: fixed by merging storage and warmup source versions and rendering `warm stale`.
14. Submit-time sharding was still `core_nt`-special-cased: fixed by accepting any DB with valid current prepared shard metadata and rejecting stale shard/warmup generations.

Remaining risk is Low: `latest-dir` lookup can be unavailable, in which case Auto warmup falls back to existing behavior instead of blocking all warmups; this preserves availability and logs the lookup failure.

## Validation

- `uv run ruff check api/routes/storage/prepare_db.py api/services/storage_data.py api/services/warmup_jobs.py api/services/k8s_monitoring.py api/tasks/storage/__init__.py api/services/auto_warmup_reconcile.py api/routes/blast/databases.py api/tests/test_storage_data.py api/tests/test_warmup_jobs.py api/tests/test_k8s_release_stale_warmup_jobs.py api/tests/test_auto_warmup.py`
- `uv run ruff check api/services/blast/task_config.py api/tests/test_blast_tasks.py` — passed.
- `PYTHONPATH=$PWD uv run pytest -q api/tests/test_blast_tasks.py::test_build_config_non_core_prepared_metadata_can_inject_partitions api/tests/test_blast_tasks.py::test_node_warmup_ready_check_rejects_stale_warm_generation api/tests/test_blast_tasks.py::test_stale_shard_generation_suppresses_sharding_options` — 3 passed.
- `PYTHONPATH=$PWD uv run pytest -q api/tests/test_blast_tasks.py api/tests/test_storage_data.py api/tests/test_warmup_jobs.py api/tests/test_k8s_release_stale_warmup_jobs.py api/tests/test_auto_warmup.py` — 139 passed.
- `PYTHONPATH=$PWD uv run pytest -q api/tests/test_storage_data.py api/tests/test_warmup_jobs.py api/tests/test_k8s_release_stale_warmup_jobs.py api/tests/test_auto_warmup.py` — 59 passed.
- `PYTHONPATH=$PWD uv run pytest -q api/tests` — 737 passed.
- `cd web && npm run build` — TypeScript and Vite build completed successfully.
- `scripts/dev/local-run.sh smoke` — 25/27 probes passed; the two failed probes were `/` and `/some/deep/spa/route` because the host-mode API proxy expected a frontend sidecar on `127.0.0.1:8081`, while this validation run used the Vite dev server path rather than the composed frontend sidecar. API probes, including `/api/blast/databases`, passed.