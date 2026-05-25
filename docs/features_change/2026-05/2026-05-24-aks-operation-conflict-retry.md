# AKS operation conflict retry

## Motivation

Azure rejects a second AKS create/update request while a previous managed cluster operation is still running. The dashboard previously published a failed provisioning state before Celery retried, making a normal ARM race look like a hard failure.

## User-facing change

When Azure returns `OperationNotAllowed` for an in-progress managed cluster operation, `provision_aks` now keeps the job in the `arm_create_or_update` phase, records `aks_operation_in_progress`, and retries after a short delay instead of publishing a failed state.

## API/IaC diff summary

- `api.tasks.azure.provision_aks` handles in-progress AKS operation conflicts as retryable progress.
- No infrastructure changes.

## Validation evidence

- `uv run pytest -q api/tests/test_azure_provision_aks.py -k "in_progress_cluster_operation or progress or resource_group"`
- `uv run ruff check api/tasks/azure/provision.py api/tests/test_azure_provision_aks.py api/run_celery_workers.py`
- `uv run python -m py_compile api/tasks/azure/provision.py api/tests/test_azure_provision_aks.py api/run_celery_workers.py`
- Restarted local worker and verified latest worker log has no `OperationNotAllowed`, `ResourceExistsError`, `missed heartbeat`, `reentrant`, or `raised unexpected` entries.