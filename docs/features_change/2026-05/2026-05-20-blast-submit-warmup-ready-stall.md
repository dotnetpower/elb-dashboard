# BLAST Submit Warmup-Ready Stall

## Motivation

Warm sharded BLAST submissions could appear stuck in the Warmup Check step even after node-local warmup readiness had already passed. The persisted job row did not keep the Celery submit task id, so stale-job reconciliation could not inspect the active task and could later mark the row as `worker_lost`.

## User-Facing Change

Submit jobs now persist the Celery task id immediately after enqueue. The execution timeline also marks the warmup readiness step complete once readiness passes, while the overall job remains running until later submit phases finish.

## API / IaC Diff Summary

- `/api/blast/submit` stores `task_id` on the `jobstate` row after a successful enqueue.
- BLAST progress payloads render `warmup_ready` as a completed `warming_up` step without marking the full job completed.
- No IaC changes.

## Validation Evidence

- `uv run pytest -q api/tests/test_smoke.py::test_blast_submit_persists_celery_task_id api/tests/test_blast_tasks.py::test_merge_progress_payload_marks_warmup_ready_step_completed`