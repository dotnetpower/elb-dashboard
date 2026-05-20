# Auto warmup reconcile SRP split

## Motivation

`api/tasks/storage.py` was mixing the Celery task entry point for Storage work with Auto warmup reconciliation policy, Kubernetes Ready-node gating, and Redis inflight dedupe. That made the storage task module harder to reason about after the Ready-node race hardening.

## User-facing change

No endpoint, task name, payload, or response shape changed. The Celery task remains `api.tasks.storage.reconcile_auto_warmup`.

## API / IaC diff summary

- Added `api/services/auto_warmup_reconcile.py` for Auto warmup reconcile policy, workload-node readiness checks, and Redis inflight locking.
- Kept `api.tasks.storage.reconcile_auto_warmup` as a thin Celery adapter that supplies credentials, the Celery `send_task` function, and the existing inflight acquire hook.
- Preserved the existing private monkeypatch surface used by auto-warmup tests.
- Updated service documentation maps.
- No IaC changes.

## Validation evidence

- `PYTHONPATH=$PWD uv run pytest -q api/tests/test_auto_warmup.py api/tests/test_warmup_route.py api/tests/test_warmup_jobs.py` -> 38 passed.
- `uv run ruff check api` -> all checks passed.
- `PYTHONPATH=$PWD uv run pytest -q api/tests` -> 710 passed.
