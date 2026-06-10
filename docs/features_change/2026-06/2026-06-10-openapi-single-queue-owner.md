# elb-openapi single queue owner (replicas 2 → 1)

## Motivation

Bursting many `POST /v1/jobs` requests at the sibling `elb-openapi` execution
plane did not queue correctly: jobs ran past the intended
`ELB_OPENAPI_MAX_ACTIVE_SUBMISSIONS` ceiling and some queued jobs never
started.

Root cause traced through the sibling `docker-openapi/app/main.py`:

- The OpenAPI service holds its job queue in a **process-local in-memory dict**
  (`_jobs`). It enforces `ELB_OPENAPI_MAX_ACTIVE_SUBMISSIONS` against that local
  view only (`_active_job_count_unlocked()` → `_claim_next_job()`), and after
  the one-time `_ensure_loaded()` startup read it **never re-reads peer
  ConfigMaps**. The dispatcher and watchdog loops do not reload all jobs.
- The dashboard deployed `elb-openapi` with `replicas: 2`. Two replicas →
  **two independent in-memory queues** behind one LoadBalancer. Effective
  run-concurrency = `replicas × MAX_ACTIVE_SUBMISSIONS` (2 × 2 = 4 instead of
  2), and a job routed to replica A is invisible to replica B forever, so a
  queued job can be stranded on a busy replica while the other sits idle.

ConfigMap persistence only serves pod-restart recovery; the read path never
consults it for admission, so it does not provide cross-replica coordination.

## User-facing change

`/v1/jobs` submissions now queue correctly: exactly one authoritative in-memory
queue owner enforces `ELB_OPENAPI_MAX_ACTIVE_SUBMISSIONS` for the whole
service. Excess submissions stay `queued` and are dispatched as running jobs
complete.

## API / IaC diff summary

`api/tasks/openapi/manifests.py` (`build_manifests`):

- `replicas: 2 → 1` (single authoritative queue owner).
- Rollout strategy `maxUnavailable:0 / maxSurge:1 → maxUnavailable:1 /
  maxSurge:0` so the old pod terminates before the new one starts — two queue
  owners never coexist mid-rollout. The brief submit-path gap is covered by the
  sibling reloading job state from its ConfigMaps on startup.
- PodDisruptionBudget `minAvailable:1 → maxUnavailable:1`. On a single replica
  `minAvailable:1` would block every voluntary node drain / AKS upgrade
  forever; `maxUnavailable:1` permits the drain (the queue owner is
  intentionally not HA and recovers from ConfigMaps on reschedule).
- Updated module docstring + inline comments + retained `topologySpread`
  (harmless on one replica; kept for a future shared-store multi-replica move).

`api/tests/test_openapi_task.py`:

- Renamed `test_build_manifests_hardens_for_ha` →
  `test_build_manifests_single_queue_owner`; asserts `replicas == 1`,
  `maxUnavailable == 1`, `maxSurge == 0`, PDB `maxUnavailable == 1` with no
  `minAvailable`.

No Bicep / Container App changes. No `build_manifests` signature change.

## Trade-off

HA is intentionally dropped for the OpenAPI submit path: a pod crash or node
drain briefly interrupts `/v1/jobs` until the single pod is rescheduled (liveness
probe restarts a wedged pod; queued/running state is restored from ConfigMaps).
This is the correct posture for a process-local in-memory queue — correctness of
the concurrency ceiling outweighs sub-minute submit-path availability. A proper
multi-replica fix would require moving the sibling's queue to a shared,
CAS-coordinated store (sibling `elastic-blast-azure` repo work, tracked
separately).

## Validation evidence

- `uv run pytest -q api/tests/test_openapi_task.py
  api/tests/test_openapi_deploy_contract.py api/tests/test_openapi_token.py` —
  31 passed.
- `uv run pytest -q api/tests -k "openapi or manifest or external_blast"` —
  288 passed.
- `uv run ruff check api/tasks/openapi/manifests.py api/tests/test_openapi_task.py`
  — all checks passed.
