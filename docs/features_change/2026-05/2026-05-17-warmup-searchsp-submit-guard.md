# Warmup Search Space Submit Guard

## Motivation

Precise sharded BLAST should not start until node-local warmup is actually ready. Sharded result equivalence also depends on sending the calibrated full-run effective search space to every shard with `-searchsp`; otherwise BLAST computes shard-local effective search spaces and E-values drift from the full DB run.

Warmup also took longer than expected on the 10 x E16 workload pool. The previous default launched 64 AzCopy workers per warmup pod, which can create about 640 concurrent transfers across the pool and increase Storage throttling risk.

## User-facing Change

- Sharded BLAST submissions now wait in Celery for node-local warmup readiness instead of proceeding against a cold or partially warmed DB.
- If warmup is still loading, the job remains retryable in the `waiting_for_warmup` phase and survives browser refresh.
- Calibrated `core_nt` search-space propagation is now covered by a regression test using the documented calibration artifact.
- New warmup Jobs use lower AzCopy defaults: 16 concurrent transfers and 2 GiB buffer per pod.

## API / Task Diff Summary

- `api.tasks.blast.submit` checks Kubernetes warmup status before sharded submit paths.
- `api.tasks.blast` now treats `sharding_mode != off` or `db_auto_partition=true` as requiring Ready node-local warmup unless `enable_warmup=false` is explicit.
- `api.services.warmup_jobs` reduces default AzCopy concurrency and buffer values for new warmup Jobs.
- Tests lock the `core_nt` calibrated `Statistics_eff-space` value `32156241807668` into the generated BLAST config as `-searchsp 32156241807668`.

## Validation Evidence

- `uv run pytest -q api/tests/test_blast_tasks.py api/tests/test_warmup_jobs.py` — 79 passed.
- `uv run ruff check api/tasks/blast.py api/services/warmup_jobs.py api/tests/test_blast_tasks.py api/tests/test_warmup_jobs.py` — passed.

## Notes

The `core_nt` value is valid for the documented calibration conditions in `docs/blast-searchsp-discovery.md`. This change does not claim every future NCBI Web BLAST configuration has the same hidden effective search space; it guarantees that when calibrated metadata supplies a full-run effective search space, the dashboard-generated sharded config applies it uniformly to every shard.
