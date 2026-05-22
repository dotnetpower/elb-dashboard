# 2026-05-22 — Split oversized `__init__.py` files in `api/tasks/`

## Motivation

Three `api/tasks/<package>/__init__.py` files had grown well past a single
responsibility — they each defined the package's helpers **and** every Celery
task body in one file:

| File | Lines (before) | Tasks defined inside |
|------|----------------|----------------------|
| `api/tasks/storage/__init__.py` | 647 | `warmup_database`, `check_database_updates`, `reconcile_auto_warmup` |
| `api/tasks/openapi/__init__.py` | 637 | `deploy_openapi_service` (plus 5 large private helpers) |
| `api/tasks/azure/__init__.py`   | 597 | `provision_aks`, `start_aks`, `stop_aks`, `delete_aks`, `assign_aks_roles`, `diag_noop` |

The charter ([.github/copilot-instructions.md](../../../.github/copilot-instructions.md))
treats the per-file context header as an SRP gate: if the `Responsibility`
needs "and" chains or multiple architectural concerns, split before adding
more code. These packages already failed that gate.

`api/tasks/blast/` was already organised this way (thin facade + sibling
submodules) — this change brings the other task packages in line.

## User-facing change

None. All public Celery task names, route imports, and test monkeypatch
targets are unchanged.

## API / IaC diff summary

For each of the three packages, the monolithic `__init__.py` was replaced
with a thin facade that re-exports the same symbols, and the task / helper
code was extracted into dedicated sibling modules:

**`api/tasks/storage/`** (50-line facade + four siblings)
* `helpers.py` — `BLAST_DATABASES`, `now_iso`, `publish_db_metadata_invalidate`, `update_state`, `record_task_progress`, `wait_for_warmup_jobs`
* `warmup.py` — `warmup_database` Celery task
* `update_check.py` — `check_database_updates` Celery task
* `reconcile.py` — `reconcile_auto_warmup` Celery task

**`api/tasks/openapi/`** (26-line facade + six siblings)
* `constants.py` — `MI_NAME`, `K8S_SA_NAME`, `K8S_NAMESPACE`, `FED_CRED_NAME`, role definition IDs
* `helpers.py` — `blast_node_count`, `now_iso`, `record_progress`
* `rbac.py` — `assign_role_idempotent`, `setup_workload_identity`
* `manifests.py` — `build_manifests`
* `kubectl.py` — `kubectl_apply`
* `deploy.py` — `deploy_openapi_service` Celery task

**`api/tasks/azure/`** (55-line facade + six siblings)
* `helpers.py` — `now_iso`, `update_state`
* `cluster_params.py` — `build_cluster_params`
* `rbac.py` — `attach_acr`, `grant_storage_blob_contributor_to_aks`, `ensure_aks_runtime_rbac`, `assign_aks_roles` Celery task
* `provision.py` — `provision_aks` Celery task
* `lifecycle.py` — `start_aks`, `stop_aks`, `delete_aks` Celery tasks
* `diagnostics.py` — `diag_noop` Celery task

### Compatibility surface preserved

* All Celery task names (`api.tasks.storage.warmup_database`,
  `api.tasks.azure.start_aks`, `api.tasks.openapi.deploy_openapi_service`, …)
  are unchanged — Celery routes / beat schedules / route callers continue to
  work without edits.
* All private helper names that tests or other tasks import directly
  (`_attach_acr`, `_grant_storage_blob_contributor_to_aks`,
  `_ensure_aks_runtime_rbac`, `_build_cluster_params`, `_build_manifests`,
  `_kubectl_apply`, `_setup_workload_identity`, `_update_state`,
  `_record_task_progress`, `_now_iso`, `_select_warmup_shard_count`,
  `_program_to_mol_type`, `_build_elb_image`, `_publish_db_metadata_invalidate`,
  `_wait_for_warmup_jobs`, `_autowarmup_inflight_acquire`) are re-exported by
  the facades and listed in `__all__` to silence the `RUF`/`F401` warnings
  flagged by `~/.config/agent/work-discipline.md` (Facade re-export note,
  2026-05-19).
* Module-level Azure SDK clients (`aks_client`, `acr_client`, `storage_client`,
  `get_credential`) are also re-exported so existing
  `monkeypatch.setattr(azure, "aks_client", ...)` patterns in
  `api/tests/test_azure_tasks.py` continue to work.

### Monkeypatch-friendly facade indirection

Tests use `monkeypatch.setattr("api.tasks.storage.<name>", ...)` and
`monkeypatch.setattr(azure, "<name>", ...)` to override helpers/clients
inside the running task body. Direct `from … import X` inside a task module
binds the original symbol into that module's namespace, so a facade-level
patch would no longer affect the call site.

To preserve the contract, the task modules now look those symbols up via
the package at call time (`import api.tasks.storage as _facade` then
`_facade._update_state(...)`). The pattern is used in
[api/tasks/storage/warmup.py](../../../api/tasks/storage/warmup.py),
[api/tasks/storage/reconcile.py](../../../api/tasks/storage/reconcile.py),
[api/tasks/storage/update_check.py](../../../api/tasks/storage/update_check.py),
[api/tasks/azure/rbac.py](../../../api/tasks/azure/rbac.py),
[api/tasks/azure/provision.py](../../../api/tasks/azure/provision.py), and
[api/tasks/azure/lifecycle.py](../../../api/tasks/azure/lifecycle.py).

## Validation evidence

* `uv run pytest -q api/tests` → **1021 passed in 32.97s** (was 1017 +
  9 failed pre-fix; the 4-test delta comes from previously-broken collection
  paths that now run).
* `uv run ruff check api` → **All checks passed!**

Largest remaining file in the touched packages:

```
475 api/tasks/storage/warmup.py      ← single Celery task body (warmup_database)
239 api/tasks/azure/rbac.py
205 api/tasks/openapi/deploy.py
198 api/tasks/openapi/manifests.py
…
 50 api/tasks/storage/__init__.py    (was 647)
 26 api/tasks/openapi/__init__.py    (was 637)
 55 api/tasks/azure/__init__.py      (was 597)
```

The largest sibling (`storage/warmup.py`, 475 lines) is the
`warmup_database` Celery task body itself — a single coherent
side-effecting operation, so it stays as one file per the
implementation-discipline rule against unnecessary abstractions.

## No infra / no UI / no deployment change

This is a pure refactor of Python module layout. No Bicep, no
Container App template, no SPA bundle, no behaviour change — so no
`scripts/dev/quick-deploy.sh` / `postprovision.sh` / `azd provision`
required per charter §13 ("Do NOT redeploy for ordinary code changes").
