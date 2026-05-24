# AKS Provisioning — Step / Pool / ARM Sub-Progress

## Motivation

The "Provisioning..." banner during AKS cluster creation showed almost nothing:
just a phase label (when present) and an elapsed-time counter. Two latent bugs
made the experience worse:

1. `provision_aks` wrote phase strings to the `JobStateRepository` via
   `helpers.update_state(...)` but never called Celery's
   `task.update_state(state="PROGRESS", meta={...})`. The
   `/api/tasks/{id}` endpoint surfaces only `result.info`, so the FE banner
   effectively saw an empty `progress` payload — the phase line stayed at the
   default "Provisioning..." until the cluster appeared in the list.
2. The long `arm_create_or_update` phase (5–10 minutes) called
   `poller.result()` synchronously with no sub-progress publish, so the user
   stared at a flat banner for most of the run with no indication of
   per-pool progress, ARM state, or cluster visibility.

## User-facing change

The cluster provisioning banner now renders:

- **Step indicator** — `Step 3/5 · Creating AKS cluster (5–10 min) · 4m 12s`
- **Sub-message** — `AKS state: Creating · ARM 1m 40s` (refreshes every 20 s
  during the ARM phase)
- **Progress bar** — interpolates within the long ARM step using
  `arm_elapsed_seconds` so the bar moves smoothly during the wait.
- **Per-pool chips** — `systempool · Succeeded · 1n` /
  `blastpool · Creating · 10n`, color-coded (success = green, in-progress =
  accent, failed = danger).

The banner degrades gracefully when fields are missing — the first poll
before the task starts publishing still shows the basic phase + elapsed UX.

## API / IaC diff

- `api/tasks/azure/helpers.py`:
  - New `record_task_progress(task, phase, **meta)` and `publish_progress(
    task, job_id, phase, *, step, total_steps, status, message, **extra)`
    helpers. Both are best-effort (catch all exceptions). `publish_progress`
    writes to *both* the state repo and Celery `result.info` in one call.
- `api/tasks/azure/provision.py`:
  - New `_PROVISION_STEPS` ordered list of `(phase, label)` driving the
    `Step N/M` indicator.
  - New `_publish(self, job_id, phase, ...)` wrapper that fills in step /
    total_steps / human label automatically.
  - New `_poll_arm_create(task, poller, ...)` that polls
    `poller.done()` every 20 s and publishes a snapshot of
    `ManagedCluster.provisioning_state` + per-AgentPool
    `provisioning_state` so the banner can render live pool chips.
    Older `poller` fakes without `.done()` skip the loop entirely so the
    test suite keeps working.
  - All five phases (`creating_cluster`, `ensuring_resource_group`,
    `arm_create_or_update`, `ensuring_rbac`, `completed`) now publish via
    `_publish`. The duplicate `update_state(...)` write per phase is gone.
- `web/src/components/cards/ClusterCard/useClusterProvisioning.ts`:
  - New `taskProgress` state (full `progress` payload) returned from the hook.
  - The task poller now writes `setTaskProgress(progress)` on every tick.
- `web/src/components/cards/ClusterCard/ProvisioningBanner.tsx`:
  - New `taskProgress` prop and `ProvisionProgress` interface.
  - Renders step indicator, sub-message, progress bar, per-pool chips.
- `web/src/components/cards/ClusterCard/ClusterCard.tsx`:
  - Threads `prov.taskProgress` into the banner.

No infra changes. No new dependencies.

## Validation

- `uv run pytest -q api/tests/test_azure_provision_aks.py` — 6 passed
  (includes the new
  `test_provision_aks_publishes_step_progress_with_pool_states` which
  asserts step / total_steps on every publish and a full pool snapshot
  during the ARM poll loop).
- `uv run pytest -q api/tests/test_azure_tasks.py
  api/tests/test_azure_provision_aks.py` — 11 passed.
- Full backend regression: 1392 passed, 1 unrelated pre-existing failure
  (`test_response_contracts.py::test_preflight_returns_admission_decision`
  is broken by uncommitted edits in `api/routes/blast/preflight.py` that
  predate this change).
- `uv run ruff check api/tasks/azure/provision.py api/tasks/azure/helpers.py
  api/tests/test_azure_provision_aks.py` — All checks passed.
- `cd web && npm run build` — built in 6.16 s, no TypeScript errors.
