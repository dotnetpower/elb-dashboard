# Large Python SRP Refactor

## Motivation

Several Python modules had accumulated unrelated responsibilities while adding BLAST submit validation, external job projection, live log streaming, and progress tracking. That made regression review harder and increased the chance that a small route or helper change would disturb unrelated behavior.

## User-Facing Change

- No intentional API or UI behavior change.
- Existing endpoints and private compatibility imports remain wired through the same package-level surfaces.

## API / IaC Diff Summary

- Split BLAST pre-flight checks from `api.routes.blast.submit` into `api.routes.blast.preflight`.
- Split external OpenAPI job cache/sync/projection helpers from `api.services.blast_job_state` into `api.services.blast_external_jobs`.
- Split BLAST task progress payload merge helpers from `api.tasks.blast.__init__` into `api.tasks.blast.progress`.
- Split Kubernetes node metrics/top parsing helpers from `api.services.k8s_monitoring` into `api.services.k8s_metrics`.
- Kept compatibility re-exports for existing private helper imports used by route packages and tests.
- No IaC changes.

## Validation Evidence

- Focused pre-flight/submit smoke tests passed.
- External job projection tests passed: `api/tests/test_local_to_blast_job.py api/tests/test_external_blast_api.py` -> 51 passed.
- BLAST progress payload tests passed as part of focused SRP validation -> 57 passed.
- Kubernetes smoke and monitoring tests passed after metrics extraction -> 73 passed.
- Backend lint: `uv run ruff check` on all changed Python modules -> passed.
- Full backend regression: `uv run pytest -q api/tests` -> 786 passed.
- VS Code diagnostics on changed Python modules -> no errors.