# BLAST Run Progress and SSD Staging

## Motivation

Run details could appear idle during early submit phases, then mark several legacy steps complete at once. The Submit Job step also hid the long node-local SSD staging work performed by ElasticBLAST, making the run look stuck.

## User-Facing Change

- Run details now uses Container Apps / AKS-oriented phases instead of legacy VM/storage/upload phases.
- Earlier running steps are marked completed as soon as the orchestrator advances to the next step.
- Node-local DB staging is represented as a dedicated `staging_db` progress step when submit requires warmed local SSD shards.
- The warmup and submit-time shard init scripts skip already warmed node-local DB files via `.download-complete`, reducing repeat submit-time staging after warmup.
- Warm cache validation now matches the prepared `core_nt` layout: `taxdb.btd` / `taxdb.bti`, non-partial `.nsq` files, source-version markers, and stale `.azDownload-*` cleanup.

## API / IaC Diff Summary

- Backend progress payload merging now follows a canonical BLAST progress order.
- The `/api/blast/jobs/{job_id}` K8s refresh path now uses the same canonical progress order when it observes that a running job has completed, so previous running steps such as `submitting` are closed instead of lingering after the overall job reaches `completed`.
- BLAST submit no longer attributes the whole `elastic-blast submit` stream to `staging_db`. Warm-cache preparation can mark `staging_db` first, but live submit output advances to `submitting` so the UI does not look stuck while ElasticBLAST waits for Kubernetes work.
- Warmup ConfigMap script generation now makes `init-db-shard-aks.sh` idempotent for direct ElasticBLAST init-SSD calls. The script changes into `/blast/blastdb` internally, accepts existing zero-byte completion markers, writes non-empty markers for new downloads, removes stale azcopy partial files, and validates the source generation when `ELB_DB_SOURCE_VERSION` is present.
- Warmup now uses a dedicated `elb-warmup-scripts` ConfigMap instead of overwriting ElasticBLAST's `elb-scripts` ConfigMap. This prevents warmed DB jobs from deleting submit-time scripts such as `query-download-ssd-aks.sh`, `results-export-aks.sh`, and `elb-finalizer-aks.sh`.
- The ElasticBLAST runtime patch now overwrites the vendored `init-db-shard-aks.sh` submit template with the same hardened skip contract. This prevents `elastic-blast submit` from replacing the warmed ConfigMap with an older script that requires `taxonomy4blast.sqlite3` or a non-empty completion marker before it can skip. When ElasticBLAST does not pass `ELB_DB_SOURCE_VERSION`, the submit-time script resolves `{db}-metadata.json` from the prepared DB container and uses its `source_version` to reject stale cache markers.
- Frontend Run details phases and messages were aligned with backend progress keys.
- No IaC resource shape changes.

## Validation Evidence

- Targeted pytest: `uv run pytest -q api/tests/test_blast_tasks.py -k 'merge_progress_payload_keeps_submit_context_and_live_output or merge_progress_payload_keeps_completed_submit_output or merge_progress_payload_completes_previous_running_steps or merge_progress_payload_completes_steps_when_phase_advances or merge_progress_payload_tracks_staging_db_before_submit'`
- Warmup script pytest: `uv run pytest -q api/tests/test_warmup_jobs.py -k warmup_scripts_configmap_contains_job_scripts`
- Warmup regression file: `uv run pytest -q api/tests/test_warmup_jobs.py` -> 22 passed.
- Warmup lint: `uv run ruff check api/services/warmup_jobs.py api/tests/test_warmup_jobs.py` -> passed.
- Submit-template regression: `uv run pytest -q api/tests/test_terminal_patch_elastic_blast.py api/tests/test_warmup_jobs.py` -> 24 passed.
- Submit-template lint: `uv run ruff check terminal/patch_elastic_blast.py api/tests/test_terminal_patch_elastic_blast.py api/tests/test_warmup_jobs.py` -> passed.
- ConfigMap split regression: `uv run pytest -q api/tests/test_terminal_patch_elastic_blast.py api/tests/test_warmup_jobs.py` -> 25 passed.
- Focused lint after ConfigMap split: `uv run ruff check terminal/patch_elastic_blast.py api/services/warmup_jobs.py api/tasks/blast/__init__.py api/tests/test_terminal_patch_elastic_blast.py api/tests/test_warmup_jobs.py` -> passed.
- Live submit `26662f8c-ee23-4aa0-9fc5-00f7586609f9` proved the old UI interpretation was misleading: `init-ssd-0..9` all reached `Complete` at `2026-05-20T06:15:48Z` / `06:15:49Z`, while the UI still showed `staging_db` until the `elastic-blast submit` command returned. The same run then failed because the warmup ConfigMap had overwritten `elb-scripts` with only `blast-vmtouch-aks.sh` and `init-db-shard-aks.sh`, so batch pods could not start `/scripts/query-download-ssd-aks.sh` and the finalizer could not start `/scripts/elb-finalizer-aks.sh`.
- Live ConfigMap repair: `kubectl create configmap elb-scripts -n default --from-file=/tmp/elb-patched-runtime.KAJo3X/src/elastic_blast/templates/scripts --dry-run=client -o yaml | kubectl apply -f -`; verified keys now include `blast-run-aks.sh`, `query-download-ssd-aks.sh`, `results-export-aks.sh`, `elb-finalizer-aks.sh`, and the hardened `init-db-shard-aks.sh` with `Resolving DB source version` and without the old `-s .download-complete` / `taxonomy4blast.sqlite3` precheck.
- Generated submit-template syntax: `bash -n` on a temporary patched `init-db-shard-aks.sh` passed; marker grep confirmed `Resolving DB source version`, `CLEANUP partial downloads`, `-f .download-complete`, `DOWNLOAD_SKIP existing shard=${ELB_SHARD_IDX}`, `taxdb.btd`, and `taxdb.bti`.
- Local terminal-exec runtime was restarted against a fresh temporary patched ElasticBLAST tree (`/tmp/elb-patched-runtime.KAJo3X` during validation). Process environment confirmed `PYTHONPATH=/tmp/elb-patched-runtime.KAJo3X/src`, and the active template contained `Resolving DB source version`, `CLEANUP partial downloads`, and `DOWNLOAD_SKIP existing shard=${ELB_SHARD_IDX}`.
- Frontend build: `cd web && npm run build`
- Live canary before hardening: `elb-cache-skip-canary-00` on `aks-blastpool-41800479-vmss00001o` still printed `Downloading manifest`, proving direct init-SSD calls were not skipping warmed shards.
- Live canary after hardening and ConfigMap refresh: `elb-cache-skip-canary-00` completed within 60 seconds and logged `CLEANUP partial downloads` followed by `DOWNLOAD_SKIP existing shard=00`; no manifest or DB copy started.
- Live full submit after the canary still recopied shards because `elastic-blast submit` overwrote the `elb-scripts` ConfigMap from its vendored template. The affected `init-ssd-*` logs printed `Downloading with pattern`, and the live ConfigMap contained the old `-s .download-complete` / `taxonomy4blast.sqlite3` checks. The final fix therefore moved the hardening into `terminal/patch_elastic_blast.py`, which is the submit-time template source.
- Live full submit `f172ae44-472a-41e6-8d02-408472d895c0` completed after the ConfigMap split and submit-template hardening: `staging_db` completed at `2026-05-20T06:39:18Z`, new init suffix `e2fc8081` advanced to 10/10 completed batch jobs, `elb-finalizer-e2fc8081` completed, and finalizer logs uploaded `merged_results.out.gz` plus `merge-report.json` under the run result prefix.
- Result API smoke for the same job with `storage_account=elbstg01` returned `manifest.status=available`, `file_count=73`, and `parseable_count=35`; alignments smoke parsed the merged result with `total_hits=100`, `files_parsed=1`, and `blob_name=f172ae44-472a-41e6-8d02-408472d895c0/job-776c62c7b5af4654813da9c3e2fc8081/merged_results.out.gz`.
- Progress refresh regression: `uv run pytest -q api/tests/test_local_to_blast_job.py api/tests/test_blast_tasks.py::test_merge_progress_payload_completes_previous_running_steps` -> 12 passed; `uv run ruff check api/services/blast_job_state.py api/tests/test_local_to_blast_job.py` -> passed.
