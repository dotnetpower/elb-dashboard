# Auto warmup Ready node guard

## Motivation

AKS can report `powerState=Running` and the expected ARM node count before every Kubernetes workload node is Ready. The auto-warmup reconciler used ARM readiness only, so it could enqueue a 10-node database warmup while only 8 workload nodes were Ready. `warmup_database` then logged the mismatch but still created an 8-way warmup plan.

## User-facing change

Auto warmup now waits until all expected workload nodes are Ready before enqueueing database warmup. If AKS was started from Azure Portal and only part of the workload pool is Ready, the reconcile result records `waiting_for_warmup_nodes` with `expected_node_count`, `ready_node_count`, and the reason `waiting for all warmup nodes`.

Manual warmup remains intentionally relaxed by default: a user-triggered warmup can still run against the currently Ready nodes. The strict guard is enabled for auto-warmup tasks via `require_all_warmup_nodes=true`.

## API / IaC diff summary

- Added a Kubernetes Ready workload-node gate behind `api.tasks.storage.reconcile_auto_warmup`; the policy now lives in `api.services.auto_warmup_reconcile`.
- Auto warmup expected node count is `AutoWarmupPreference.num_nodes` when configured, otherwise the ARM `node_count`.
- Added a strict `require_all_warmup_nodes` option to `warmup_database`; auto-warmup passes it, manual warmup does not.
- When strict mode sees fewer Ready nodes than requested, `warmup_database` returns `status=deferred`, records phase `waiting_for_warmup_nodes`, and does not call `build_warmup_job_plan`.
- No IaC changes.

## Validation evidence

- `PYTHONPATH=$PWD uv run pytest -q api/tests/test_auto_warmup.py` -> 9 passed.
- `PYTHONPATH=$PWD uv run pytest -q api/tests/test_warmup_route.py api/tests/test_warmup_jobs.py` -> 29 passed.
- `uv run ruff check api/tasks/storage.py api/tests/test_auto_warmup.py` -> all checks passed.
- `uv run ruff check api` -> all checks passed.
- `PYTHONPATH=$PWD uv run pytest -q api/tests` -> 710 passed.
