# AKS provision RBAC hardening + progress visibility

## Motivation

Audit of the provision flow uncovered five gaps in the `ensuring_rbac` step
that the dashboard runs after AKS `managed_clusters.begin_create_or_update`
returns:

1. **Silent runtime-RBAC failure.** `ensure_aks_runtime_rbac` recorded
   `AcrPull` / `Storage Blob Data Contributor` failures into `roles_failed`
   and the `provision_aks` task still finished as `completed`. The cluster
   then shipped to the SPA as "Cluster ready", but the kubelet identity
   silently lacked AcrPull (ImagePullBackOff at first BLAST submit) or
   Storage Blob Data Contributor (AuthorizationPermissionMismatch on first
   data-plane call).
2. **Preflight did not verify User Access Administrator (UAA).** The
   provision task itself needs UAA on the ACR / Storage scopes to assign
   the kubelet identity its runtime roles. The preflight checked only
   `Contributor` on the cluster RG (for `managedClusters/write`).
3. **Duplicate `managed_clusters.get` round trips.** `attach_acr` and
   `grant_storage_blob_contributor_to_aks` each fetched the cluster to read
   `kubelet_oid`, adding ~2-3 s of avoidable latency to the step.
4. **No retry on Entra ID propagation.** The freshly-minted kubelet
   identity occasionally hits `PrincipalNotFound` from the Authorization
   service for the first few seconds; the assignment failed instead of
   waiting for the standard ~30-60 s propagation window.
5. **No sub-phase progress.** The UI banner sat on "Granting role
   assignments" with the same elapsed timer for the entire RBAC step,
   making it indistinguishable from a hang.

## User-facing change

* The provision task now **fails fast** when any kubelet role assignment
  fails. The cluster card surfaces the canonical error card with the role
  names, the assigned-so-far list, and the remediation pointer (run
  `/api/aks/assign-roles` after granting UAA on the right scope) instead
  of "Cluster ready" hiding a broken cluster.
* The `+ Add Cluster` preflight modal gains a new **Runtime RBAC** row that
  reports `ok` / `warn` for "User Access Administrator on
  AcrPull / Storage Blob Data Contributor target scopes". `warn` does not
  block submit (Azure RBAC is still the ground truth and we may not see a
  covering grant), but it tells the operator up-front when the
  `ensuring_rbac` step is going to fail.
* The provisioning banner shows sub-phase labels during the RBAC step
  (`Granting AcrPull to AKS kubelet on <acr>` /
  `Granting Storage Blob Data Contributor on <storage>`) under the same
  "Step 4/5" indicator, so the user sees the role currently being granted
  instead of a blank pause.
* Transient `PrincipalNotFound` during the kubelet-identity propagation
  window is now absorbed by an exponential-backoff retry (capped at 60 s
  total, 10 s max delay).
* The standalone `/api/aks/{cluster}/assign-roles` task (Re-assign roles
  affordance) also surfaces partial failures as task `FAILURE` instead of
  returning `status: completed` with a quiet `roles_failed[]`.

## API / IaC diff summary

* [api/tasks/azure/rbac.py](../../../api/tasks/azure/rbac.py)
  * New `_resolve_kubelet_oid` helper — single cluster `get` call shared
    by the two role-assignment helpers.
  * New `_create_role_assignment_with_retry` helper — idempotency
    (`RoleAssignmentExists` / `Conflict` → success) plus exponential
    backoff retry on `PrincipalNotFound` / "does not exist in the
    directory" with a 60 s deadline.
  * `attach_acr` / `grant_storage_blob_contributor_to_aks` now accept an
    optional keyword-only `kubelet_oid` so the caller can pass the
    pre-resolved value; fall back to the legacy lookup when omitted
    (preserves the standalone-call signature).
  * `ensure_aks_runtime_rbac` resolves `kubelet_oid` once, threads it
    through both helpers, and accepts a `progress_callback(phase, msg)`
    so the provision task can publish sub-phase progress without coupling
    the helper to Celery internals. The old non-fatal failure path is
    intentional — the caller is now expected to fail fast on a non-empty
    `roles_failed`.
  * `assign_aks_roles` Celery task raises `RuntimeError` when
    `roles_failed` is non-empty so the SPA polling `/api/tasks/{id}` sees
    `FAILURE` instead of "completed".
* [api/tasks/azure/provision.py](../../../api/tasks/azure/provision.py)
  * New `_RBAC_SUB_PHASES` dict; `_publish` resolves sub-phase names to
    the parent `ensuring_rbac` step number so they render under "Step
    4/5" in the banner.
  * Provision-task RBAC site now passes `progress_callback=_rbac_progress`
    into `_ensure_aks_runtime_rbac`. A non-empty `roles_failed` emits a
    `_publish("failed", ...)` and raises `RuntimeError` so Celery marks
    the task `FAILURE` (replaces the old `rbac_ensure_failed_nonfatal`
    non-fatal advisory).
* [api/services/rbac_preflight.py](../../../api/services/rbac_preflight.py)
  * Added `_ROLE_USER_ACCESS_ADMINISTRATOR` and
    `_ROLE_ASSIGNMENT_WRITE_ROLES` constants.
  * New `aks_runtime_rbac_check(...)` function — verifies UAA (or Owner)
    covers every requested runtime-RBAC target scope. Emits a
    `rbac_runtime` preflight row (status `ok` / `warn`, never `fail`),
    with a copy-pasteable `az role assignment create` remediation in
    `details.missing[]`.
* [api/services/aks_availability.py](../../../api/services/aks_availability.py)
  * `run_provision_preflight` takes new optional `acr_*` / `storage_*`
    kwargs and appends the runtime RBAC row to the existing `checks[]`.
* [api/routes/aks/preflight.py](../../../api/routes/aks/preflight.py)
  * Route forwards `acr_resource_group`, `acr_name`,
    `storage_resource_group`, `storage_account` from the request body
    to the preflight service.
* [web/src/api/aks.ts](../../../web/src/api/aks.ts)
  * `AksPreflightRequest` gains optional `acr_*` / `storage_*` fields.
* [web/src/components/cards/ClusterCard/useClusterProvisioning.ts](../../../web/src/components/cards/ClusterCard/useClusterProvisioning.ts)
  * Both preflight call sites (debounced auto-preflight and the explicit
    Create click) include ACR / Storage targets.
* [web/src/components/cards/ClusterCard/ProvisioningBanner.tsx](../../../web/src/components/cards/ClusterCard/ProvisioningBanner.tsx)
  * `PHASE_LABELS` adds entries for `ensuring_rbac_acr` /
    `ensuring_rbac_storage`.

## Validation

* `uv run pytest -q api/tests` — **1517 passed**, no regressions
  (covers `test_azure_tasks.py::test_ensure_aks_runtime_rbac_*` +
  6 new tests for sub-phase publish, narrowed-TypeError fallback,
  `PrincipalNotFound` retry, idempotent conflict, and
  `assign_aks_roles` task fail-fast;
  `test_rbac_preflight.py` + 6 new tests for `aks_runtime_rbac_check`
  including the prefix-match regression;
  `test_azure_provision_aks.py`,
  `test_aks_availability.py::test_run_provision_preflight_*`,
  `test_warmup_route.py`).
* `uv run ruff check api` — **All checks passed**.
* `cd web && npm run build` — clean build.

## Self-review fixes (post-implementation)

1. **`target_scope.startswith(grant_scope)` path-segment bug** — a UAA
   grant at `/...resourceGroups/rg` was matching a target inside
   `/...resourceGroups/rg-acr/...` (two unrelated RGs share a string
   prefix). Replaced with strict equality OR `target == grant + "/..."`
   path-segment check. Regression test:
   `test_runtime_rbac_prefix_match_does_not_false_positive`.
2. **Broad `except TypeError` fallback in `ensure_aks_runtime_rbac`** —
   was retrying ANY TypeError as if it were a legacy-signature mismatch,
   masking genuine bugs inside `attach_acr` / `grant_storage_*`.
   Refactored into `_call_with_optional_kubelet_oid` helper that only
   falls back when the message contains `"kubelet_oid"`. Regression
   test: `test_ensure_aks_runtime_rbac_does_not_swallow_internal_typeerror`.
3. **Case-inconsistency in `_is_principal_propagation_error`** — mixed
   case-sensitive (`"principalId" in msg`) and case-insensitive checks.
   Normalised to lowercase-once-then-substring-check. No behaviour
   regression test needed since the function was simplified.

## Out of scope

* No Bicep change. Existing deployments that already grant the dashboard
  UAMI UAA at the dashboard RG (via `controlPlaneRoles.bicep`) and at the
  workload cluster RG (via `workloadClusterRoles.bicep`) continue to
  work; the new `rbac_runtime` row stays `ok` for them.
* `start_aks` and its `auto_openapi` follow-on are unchanged — they
  enqueue `assign_aks_roles`, which now propagates failure as Celery
  `FAILURE` automatically.
