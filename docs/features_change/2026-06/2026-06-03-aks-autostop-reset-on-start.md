# AKS idle auto-stop no longer stops a freshly started cluster

## Motivation

A user started an AKS cluster from the dashboard and it stopped again within a
few minutes. Production logs for `elb-cluster-02` (idle window 15 min) showed the
idle auto-stop loop repeatedly stopping the cluster right after each start
(stops at 06:48, 08:20, 13:21) and at 13:21:22 a Celery task **ERROR**:

```
ResourceExistsError: (OperationNotAllowed) ... in progress start managed cluster ...
```

followed by a manual restart at 13:27. Two distinct defects were confirmed:

1. **A cluster start did not reset the idle clock.** The evaluator anchors the
   idle deadline on the most recent BLAST job timestamp (or `pref.created_at`).
   When the last job was older than `idle_minutes`, the very next beat tick
   (≤ 5 min) computed a deadline already in the past and decided `stop` — so the
   cluster was torn down moments after the user started it.
2. **Stopping mid-start crashed the task.** AKS `power_state.code` reports
   `"Running"` the instant a start LRO begins, while `provisioning_state` is
   still `"Starting"`. The evaluator's only power gate saw `"Running"`, issued a
   stop, and ARM rejected it with `OperationNotAllowed` — surfacing as a Celery
   task ERROR.

## User-facing change

* Starting a cluster (via `start_aks`) now grants it a full `idle_minutes` grace
  window before the idle auto-stop loop may stop it again. No more "started, then
  immediately stopped" surprises.
* The auto-stop act task no longer attempts to stop a cluster whose
  `provisioning_state` is anything other than `Succeeded` (e.g. `Starting`,
  `Updating`), removing the `OperationNotAllowed` error class from the worker.

No SPA / API response shape changed — the new preference field is internal and is
not projected by the public `_PUBLIC_PREF_FIELDS` allowlist on
`/api/aks/*/autostop`.

## API / IaC diff summary

* `api/services/auto_stop.py`
  * `AutoStopPreference` gains `last_started_at: str = ""` (round-trips through
    `to_dict` / `from_dict`).
  * New CAS-safe helper `mark_auto_stop_started(subscription_id, resource_group,
    cluster_name) -> AutoStopPreference | None` — stamps `last_started_at` /
    `updated_at`; no-op (returns `None`) when no preference row exists.
* `api/services/auto_stop_evaluator.py`
  * `evaluate_cluster(...)` gains an optional `provisioning_state: str = ""`
    keyword and keeps the cluster (`reason="provisioning:<state>"`) when it is
    set and not `succeeded`.
  * The idle anchor now also considers `pref.last_started_at`, so a recent start
    extends the deadline even when no jobs are observed.
* `api/services/cluster_health.py`
  * `ClusterHealth` gains `provisioning_state: str | None`, populated from the
    cluster meta snapshot on every return path.
* `api/tasks/azure/idle_autostop.py`
  * New `_provisioning_state(pref)` helper (shares the 90 s cluster-health cache,
    non-fatal). The act task passes it into `evaluate_cluster`. The beat decide
    path is intentionally unchanged.
* `api/tasks/azure/lifecycle.py`
  * `start_aks` calls `mark_auto_stop_started(...)` before issuing the start LRO,
    stamping the anchor as early as possible to close the decide-vs-act race.

No Bicep / IaC change.

## Validation evidence

* `uv run pytest -q api/tests` → 2439 passed, 3 skipped (one pre-existing flaky
  `test_terminal_exec.py::test_run_truncates_stdout_above_cap` under parallelism;
  passes in isolation, unrelated to this change).
* `uv run ruff check api` → All checks passed.
* New / updated tests:
  * `api/tests/test_auto_stop.py` — `mark_auto_stop_started` stamps + persists,
    no-op without a row, `last_started_at` dict round-trip.
  * `api/tests/test_auto_stop_evaluator.py` — recent start resets the idle clock,
    stale start does not block stop, recent start anchors when no jobs observed,
    transitional `provisioning_state` keeps the cluster, `Succeeded` allows stop.
  * `api/tests/test_azure_tasks.py` — `start_aks` stamps `last_started_at`.
  * `api/tests/test_cluster_health.py`, `api/tests/test_prepare_db_aks_route.py`,
    `api/tests/test_auto_stop_task.py` — updated for the new `provisioning_state`
    field / parameter.

Per charter §13 this fix is validated by tests + reasoning and is **not**
redeployed here; it lands on the next normal deploy.
