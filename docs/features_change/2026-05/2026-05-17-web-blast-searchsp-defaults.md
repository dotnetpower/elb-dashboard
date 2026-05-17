# 2026-05-17 — Web BLAST searchsp defaults

## Motivation

Precise BLAST sharding must use the same effective search space on every shard
to match a full-database or NCBI Web BLAST-equivalent statistical model. The
`core_nt` calibration measured a full-database `Statistics_eff-space` of
`32156241807668`, but the submit UI and pre-flight path still required callers
to provide that value manually.

## User-facing Change

- The BLAST database list now exposes verified Web BLAST-compatible search-space
  metadata for calibrated databases.
- Selecting `core_nt` automatically sends `db_effective_search_space =
  32156241807668` in pre-flight and submit payloads.
- The Algorithm Parameters panel shows the selected database's calibrated search
  space when one is available.
- Users can still override the automatic default by putting an explicit
  `-searchsp` value in Additional options.
- Pre-flight now accepts the UI's `aks_cluster_name` field, matching the submit
  route normalization.

## API / Task Diff Summary

- Added `api.services.web_blast_searchsp` as the single source of verified
  database defaults.
- Enriched `api.services.storage_data.list_databases` rows with
  `web_blast_searchsp`, scope, and evidence fields.
- Updated `/api/blast/pre-flight` and `/api/blast/jobs` normalization to inject
  the verified default only when no explicit search-space override is present.
- Added `searchsp` alias handling for older payloads, mapping it to
  `db_effective_search_space` at the HTTP boundary.
- Wired the React submit page to include the database default in pre-flight and
  submit requests.

## Validation Evidence

- Reset local Celery/Redis state by deleting/recreating `elb-dev-redis`; queues
  were empty after restart.
- Restarted local API, worker, beat, web, and terminal-exec under
  `.logs/local/20260517T035004Z-1039155/`.
- `curl http://127.0.0.1:8085/api/health` returned `status: ok`.
- `curl http://127.0.0.1:8085/api/health/celery` showed the worker registered
  `api.tasks.blast.submit` and queue lengths of `0`.
- `POST /api/health/celery/enqueue-noop?message=searchsp-reset-check` completed
  with Celery state `SUCCESS`.
- `api.services.terminal_exec.run(["elastic-blast", "--version"])` returned
  `elastic-blast 1.5.0.post63+e3e9f51`.
- `POST /api/blast/pre-flight` with `aks_cluster_name`, `core_nt`, precise
  sharding, and no explicit `db_effective_search_space` returned `ready: true`.
- `GET /api/blast/databases` returned `core_nt.web_blast_searchsp =
  32156241807668`.
- Existing real AKS evidence: `blastn-batch-s00..s09-job-000-138d8383` were
  `Complete`; shard pod logs showed BLAST running with
  `-searchsp 32156241807668`; shard XML reported
  `Statistics_eff-space = 32156241807668`.
- Fresh real AKS evidence: dashboard job
  `d07a6a3a-208d-4606-87ff-33304bc7e7dd`, ElasticBLAST job
  `job-9c58c936101042ea996681485be97da5`, and Kubernetes jobs
  `blastn-batch-s00..s09-job-000-5be97da5` all used the injected
  `-searchsp 32156241807668`. The shard jobs completed in `17s` to `19s` on
  warmed local SSD nodes.
- The latest shard job manifest used hostPath `/workspace` mounted at
  `/blast/blastdb` with `subPath: blast`; the only init container was
  `import-query-batches`, which completed in about 2 seconds. No DB download
  init container ran on the warmed path.
- Patched and live-reran `elb-finalizer-5be97da5` after fixing the finalizer
  image and azcopy wildcard. The finalizer completed, downloaded 10 shard XML
  files, merged `0` hits from `1` query, uploaded `merged_results.out.gz` and
  `merge-report.json`, and wrote `metadata/SUCCESS.txt`. The final rerun after
  XML DB-name normalization completed in `46s`.
- Final evidence is in
  `docs/temp/core-nt-searchsp/fresh-2026-05-17/live-finalizer-5be97da5/`.
  `merged_vs_baseline_stats.json` reports `all_result_statistics_match: true`
  against the full DB baseline: `db_len=1041443571674`, `db_num=125619662`,
  `eff_space=32156241807668`, `hsp_len=33`, `hit_count=0`, and `hsp_count=0`.
  The merged top-level `BlastOutput_db` is normalized to `core_nt`; the XML is
  not byte-identical to the VM baseline because the baseline contains the local
  filesystem DB path.
- Browser verification on `http://127.0.0.1:8090/blast/submit` selected
  `core_nt` and showed the submit summary with
  `Searchsp: 32156241807668`.
- Fresh smoke submit `b9a1c180-06a9-449d-b12f-aefc12ff42bc` for
  `16S_ribosomal_RNA` verified query upload, result metadata creation, and
  terminal-exec launch of `elastic-blast submit`. It was cancelled before BLAST
  pod execution because PVC `blast-dbs-pvc-rwm` stayed Pending with
  `storageclass.storage.k8s.io "azureblob-nfs-premium" not found`. Leftover
  `job/init-pv` and `pvc/blast-dbs-pvc-rwm` were deleted after evidence capture.
- The job detail page refreshed to `failed / submit_failed` and displayed the
  persisted submit error summary instead of the empty fallback message.
- `uv run pytest -q api/tests/test_blast_submit_route_options.py api/tests/test_smoke.py -q` — passed.
- `uv run pytest -q api/tests/test_blast_submit_route_options.py api/tests/test_smoke.py api/tests/test_storage_data.py api/tests/test_blast_config_sharding.py api/tests/test_sharded_merge.py` — 94 passed.
- `uv run ruff check api/services/web_blast_searchsp.py api/services/storage_data.py api/tests/test_blast_submit_route_options.py api/tests/test_smoke.py` — passed.
- `uv run pytest -q api/tests/test_sharded_merge.py` — 3 passed.
- `uv run ruff check api/services/web_blast_searchsp.py api/services/storage_data.py api/tests/test_sharded_merge.py terminal/patch_elastic_blast.py` — passed.
- `get_errors` for `terminal/merge-sharded-results.sh`,
  `terminal/patch_elastic_blast.py`, `api/services/web_blast_searchsp.py`, and
  `api/tests/test_sharded_merge.py` — no errors found.
- `cd web && npm run test -- src/pages/blastSubmit/taxonomyFilter.test.ts` — passed.
- `cd web && npm run build` — passed with the existing Vite chunk-size warning.

## Notes

Only `core_nt` has a verified default in this change. Other databases remain
unset until their own repeated Web BLAST/ElasticBLAST evidence is captured.