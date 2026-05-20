# BLAST job artifacts fast path

## Motivation

Completed BLAST job pages could become slow when Execution Steps, result manifests, and analytics were rebuilt from large Table payloads or result blobs on every page open. Submit also performed a synchronous AKS capacity pre-check that could make the user wait before the job row existed.

## User-facing change

- BLAST submit now keeps the request path focused on validation, job-state creation, and Celery enqueue. The synchronous AKS capacity check is opt-in via `BLAST_SUBMIT_SYNC_CAPACITY_CHECK=true`.
- Completed job Execution Steps can be read through `GET /api/blast/jobs/{job_id}/execution-steps`, which prefers a small persisted artifact and falls back to the existing job payload.
- Result manifest and default analytics endpoints prefer background-built job artifacts before parsing raw result blobs.
- The results page uses the Execution Steps snapshot for terminal jobs while keeping live polling for running jobs.
- Artifact state is now explicit (`missing`, `pending`, `ready`, `failed`) so terminal pages can keep rendering while background artifacts are still building or have degraded.

## API / IaC diff summary

- Added `api.services.job_artifacts` for JSON artifact writes/reads and `jobartifactstate` metadata rows.
- Added `api.services.blast_result_artifacts` for background result manifest, aggregate, default alignments, and taxonomy artifacts.
- Added `api.tasks.blast_artifacts.finalize_job_artifacts` and enqueue hooks for terminal BLAST states.
- Added `api/run_celery_workers.py` so latency-critical queues and artifact queues run in separate Celery worker processes inside the worker sidecar.
- Added `JobStateRepository.get_summary()` for payload-free authorization and hot metadata reads.
- Added `jobartifactstate` table and `job-artifacts` container to `infra/modules/storageState.bicep`.
- Added the `blast-artifacts` queue to local and Container App worker queue lists.
- Added a finalizer sentinel artifact row to suppress duplicate terminal backfill enqueues while still allowing retry after failed or stale pending states.
- Added ordered submit log chunk artifacts under `execution-steps/logs/...` so Table state can stay compact while full terminal output is preserved.
- Changed aggregate artifact and live fallback aggregation to stream per-file stats without retaining the full hit list in request memory.
- Added gzip artifact read support and best-effort container creation for local/dev storage bootstrap.

## Validation evidence

- `uv run ruff check api/services/job_artifacts.py api/services/blast_result_artifacts.py api/tasks/blast_artifacts.py api/routes/blast/jobs.py api/routes/blast/results.py api/tasks/blast/__init__.py api/routes/blast/submit.py api/tests/test_job_artifacts.py`
- `uv run pytest -q api/tests/test_job_artifacts.py api/tests/test_local_to_blast_job.py api/tests/test_blast_results_routes.py api/tests/test_blast_tasks.py` -> 129 passed
- `cd web && npm run build` -> Vite build completed
- Hardening focused check: `uv run pytest -q api/tests/test_job_artifacts.py` -> 8 passed
- Hardening lint: `uv run ruff check api/tests/test_job_artifacts.py api/services/job_artifacts.py api/services/blast_result_artifacts.py api/routes/blast/results.py api/routes/blast/jobs.py api/tasks/blast_artifacts.py api/tasks/blast/__init__.py api/run_celery_workers.py` -> passed
- Final hardening validation: `uv run ruff check api` -> passed
- Final hardening validation: `uv run pytest -q api/tests` -> 774 passed
- Final hardening validation: `az bicep build --file infra/main.bicep --outfile /tmp/elb-dashboard-main.json` -> passed, Bicep upgrade notice only
- Final hardening validation: `cd web && npm run build` -> passed, existing Vite large chunk warning only
