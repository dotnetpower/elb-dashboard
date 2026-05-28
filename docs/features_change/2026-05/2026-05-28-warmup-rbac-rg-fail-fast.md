---
title: Warmup / RBAC silent-wrong-RG fail-fast hardening
description: Replace `X_resource_group or resource_group` silent fall-backs with explicit fail-fast validation so misrouted RBAC role assignments stop hiding behind quiet ARM 404s.
tags:
  - operate
  - security
  - infra
---

# 2026-05-28 — Warmup / RBAC silent-wrong-RG fail-fast hardening

## Motivation

The 2026-05-28 10–11 KST incident sweep traced the auto-warmup quiet
`role_summary: {status: failed}` to a silent fallback chain:

1. `api/services/auto_warmup_reconcile.py` enqueued `warmup_database`
   without forwarding `pref.storage_resource_group` — kwargs were
   dict-spread without listing the field explicitly, so the
   reconciler caller dropped it.
2. `api/tasks/storage/warmup.py` then computed
   `storage_resource_group or resource_group`, falling back to the
   **AKS cluster RG** (`rg-elb-cluster`) instead of the Storage RG
   (`rg-elb-dashboard`). The ARM call against the wrong RG raised a
   `ResourceNotFoundError`, was swallowed by the outer
   `except Exception`, and the kubelet ended up with no
   `Storage Blob Data Contributor` — but the SPA showed
   "Cluster ready".

This is a *class* of bug, not one bug: identical
`X_resource_group or resource_group` fall-backs existed in three
other production code paths (cluster provision, "Re-assign roles"
button, OpenAPI workload-identity setup) and in the manual warmup
HTTP route. Every one of them could mask a misrouted RBAC grant.

## User-facing change

There is **no behaviour change for correctly-configured tenants**
(`storage_resource_group` already flows end-to-end from the SPA
forms). For misconfigured tenants the dashboard now surfaces an
explicit failure with an actionable message instead of marking the
cluster "ready" while the kubelet silently lacks RBAC. The SPA's
existing degraded-banner / error toast wiring renders the new
fail-fast message verbatim.

## API / IaC diff summary

### Backend

* `api/tasks/storage/warmup.py`
  * Top-of-function fail-fast in `warmup_database` when
    `storage_resource_group` is missing — the validation runs
    *before* the outer `except Exception` so it cannot be swallowed.
  * Inside the RBAC ensure block, the `acr_resource_group or
    resource_group` fall-back was replaced with an explicit
    `RuntimeError` mirroring the storage-RG contract.
* `api/services/auto_warmup_reconcile.py`
  * The `send_task("api.tasks.storage.warmup_database", kwargs={…})`
    call now lists `storage_resource_group` explicitly between
    `storage_account` and `database_name`, with an inline comment
    explaining the recurrence vector.
* `api/routes/warmup.py`
  * The manual `/warmup/start` route now forwards
    `body.get("storage_resource_group", "")` to the task so the new
    fail-fast guard receives the SPA's value (it was previously
    dropped, which would have broken every manual warmup once the
    guard landed).
* `api/tasks/openapi/rbac.py`
  * `setup_workload_identity` now records
    `roles_failed["StorageBlobDataContributor"]` with a clear
    "storage_resource_group is required" message instead of falling
    back to the cluster RG and producing a confusing ARM 404. The
    function's existing "raise RuntimeError on role failure"
    contract carries the explicit failure up to
    `deploy_openapi_service`, which already surfaces it as
    `status: failed`.
* `api/tasks/azure/provision.py` and `api/tasks/azure/rbac.py`
  * Both call sites of `_ensure_aks_runtime_rbac` drop the
    `storage_resource_group or resource_group` fall-back. The
    callee already has its own env-based default
    (`AZURE_RESOURCE_GROUP` / `STORAGE_ACCOUNT_NAME`); the silent
    cluster-RG fall-back was overriding that default and routing
    the role assignment at the wrong scope.
* `api/routes/aks/openapi.py`
  * `aks_openapi_proxy` 503 response now carries a `Retry-After: 15`
    HTTP header and a `retry_after_seconds: 15` body field so SPA
    consumers can back off instead of polling tight when the
    elb-openapi Service IP is still being provisioned.

### Tests

* `api/tests/test_auto_warmup.py`
  * Asserts the reconciler forwards `storage_resource_group=rg-elb`
    on enqueue.
  * New `test_warmup_database_fails_fast_when_storage_resource_group_missing`
    pins the fail-fast contract; uses a patched
    `list_databases` that must not be called, proving the
    validation runs before any Storage access.
  * The pre-existing
    `test_warmup_database_auto_strict_waits_for_requested_ready_nodes`
    now passes `storage_resource_group="rg-elb"` to satisfy the
    new guard before it reaches the deferred-path assertions.
* `api/tests/test_warmup_database_readiness.py`
  * The three readiness tests
    (`copying` / `partial` / `update_in_progress`) now pass
    `storage_resource_group="rg-elb"` so the new fail-fast does
    not short-circuit them before the readiness checks run.

## SPA fall-backs left in place (intentional)

`web/src/components/WarmupSection.tsx`,
`web/src/components/ClusterItem/ClusterItem.tsx`, and
`web/src/components/cards/ClusterCard/useClusterProvisioning.ts`
still carry `storageResourceGroup || resourceGroup` fall-backs.
Removing them would regress single-RG deployments (the most common
demo configuration) without adding safety: the backend's new
`roles_failed` → `RuntimeError` path makes the *silent skip*
impossible. A misconfigured fallback now produces an explicit ARM
error with the wrong RG name in it, which is the desired UX —
the dashboard tells the operator exactly which RG was tried.

## Validation evidence

* `uv run pytest -q api/tests` → **1551 passed, 0 failed** (the
  pre-existing flaky `test_run_truncates_stdout_above_cap` env
  case is unrelated and intermittent).
* `uv run pytest -q api/tests/test_auto_warmup.py
  api/tests/test_warmup_route.py api/tests/test_azure_tasks.py
  api/tests/test_azure_provision_aks.py
  api/tests/test_openapi_deploy_contract.py
  api/tests/test_rbac_preflight.py` → **73 passed**.
* `uv run ruff check` over the seven touched modules →
  *All checks passed*.
* `git diff --stat` confirms only the nine intended files
  (seven api / two tests) were modified.
