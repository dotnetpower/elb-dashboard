# Cluster provisioning: ARM eventual-consistency guard for the RG ensure step

## Motivation

After `bc0fcf1` (`fix(aks): ensure resource group exists before AKS
provisioning`) the `provision_aks` Celery task calls
`resource_groups.create_or_update(...)` before the AKS create. That removed
the most common failure mode (RG never existed on a fresh subscription), but
operators still occasionally saw the same exact error after a redeploy:

```
Provisioning task failed: (ResourceGroupNotFound) Resource group
'rg-elb-cluster' could not be found.
Code: ResourceGroupNotFound
```

Root cause: ARM resource-group create returns 200 OK as soon as the row is
written to the ARM control plane, but downstream control planes (notably
AKS) occasionally still return `ResourceGroupNotFound` for a brief window
before the metadata propagates. When that window lines up with the AKS
`begin_create_or_update` call, the task fails ~10 minutes in with the same
error the original fix was meant to prevent.

## User-facing change

Cluster creation from the SPA's Cluster card no longer trips on the ARM
propagation race. The task surfaces an `ensuring_resource_group` state, then
polls `resource_groups.get(...)` until the RG is visible (up to 12 attempts
× 5 s = 60 s) before handing off to AKS. On the happy path the very first
`get` succeeds and the wait is sub-second.

## API / IaC diff summary

* `api/tasks/azure/provision.py`
  * `import time` + `from azure.core.exceptions import ResourceNotFoundError`.
  * After the existing `rc.resource_groups.create_or_update(...)`, poll
    `rc.resource_groups.get(resource_group)` with `_RG_VISIBILITY_ATTEMPTS`
    attempts and `_RG_VISIBILITY_DELAY_SECONDS` between retries. Logs each
    waiting attempt; lets the final `ResourceNotFoundError` propagate so
    Celery's existing retry policy (`autoretry_for=(Exception,)`,
    `retry_backoff`) takes over.
* `api/tests/test_azure_provision_aks.py`
  * `FakeResourceGroups.get(...)` added so the existing call-order test
    stays in sync with the new step.
  * New `test_provision_aks_retries_when_rg_not_yet_visible` pins the retry
    contract: 3 `get` attempts (2 `ResourceNotFoundError` + 1 OK), 2
    sleeps, and the AKS create only fires after the last successful `get`.

No IaC change.

## Validation

* `uv run pytest -q api/tests/test_azure_provision_aks.py` → 4 passed in
  ~4 s.
* `uv run ruff check api/tasks/azure/provision.py
  api/tests/test_azure_provision_aks.py` → All checks passed.
* No redeploy required for the previously deployed `bc0fcf1` image to keep
  working; rolling the worker sidecar with this commit picks up the new
  guard.
