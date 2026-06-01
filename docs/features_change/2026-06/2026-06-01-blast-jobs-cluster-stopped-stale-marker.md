# BLAST Jobs list: mark active rows stale when the cluster is stopped

## Motivation

`GET /api/blast/jobs` refreshes each active (`running` / `submitted`) row
against the Kubernetes API before responding, so the list page flips a finished
job to `completed` without waiting for the 60 s beat reconcile. When the job's
AKS cluster is **stopped** (or deleted), that K8s refresh cannot succeed: it
either hits the cached last-known state or pays a ~10 s connection timeout per
row, and the SPA keeps rendering a stale `running` status as if the job were
still making live progress. A researcher looking at the dashboard had no signal
that the job is actually frozen because the cluster is powered off.

## User-facing change

- The Jobs list now consults cached ARM cluster health
  (`get_cluster_health`, 90 s TTL, one call per distinct
  subscription/resource-group/cluster scope) **before** the K8s refresh.
- For an active row whose cluster reports `healthy=False` (reason
  `cluster_stopped` or `cluster_not_found`):
  - the expensive K8s refresh is **skipped** (no ~10 s per-row timeout), and
  - the row is tagged with `stale: true`, `refresh_blocked_reason`, and
    (when known) `cluster_power_state`.
- The BlastJobs table renders a small uppercase indicator next to the phase
  pill: `❄ FROZEN` for a stopped cluster, `✕ NO CLUSTER` for a missing one,
  with a tooltip explaining the status is frozen until the cluster restarts.
- Behaviour is unchanged for healthy clusters and for terminal rows
  (`completed` / `failed` are never tagged stale). All new response fields are
  optional/nullable — existing consumers are unaffected.

## API / IaC diff summary

Backend (`api/`):

- `api/services/blast/job_state.py`
  - `_local_to_blast_job(...)` gains optional kwargs
    `refresh_blocked_reason: str | None`, `cluster_power_state: str | None`;
    sets `stale` / `refresh_blocked_reason` / `cluster_power_state` only for
    active (`running` / `submitted`) rows.
  - new `_row_refresh_scope(state)` extracts (sub, rg, cluster) from indexed
    columns with payload fallback.
  - new `_blocked_refresh_reasons(rows)` groups active rows by cluster scope,
    lazily probes `get_cluster_health`, and returns
    `{job_id: ClusterHealth}` only for unhealthy clusters. Best-effort:
    returns `{}` when there are no active rows / no usable scope / no
    credential.
- `api/routes/_blast_shared.py` re-exports `_blocked_refresh_reasons`.
- `api/routes/blast/jobs.py` (`blast_jobs_list`) computes
  `blocked_refresh = _blocked_refresh_reasons(rows)`, `continue`s past the K8s
  refresh for blocked rows, and forwards the health reason / power_state into
  `_local_to_blast_job`.

Frontend (`web/`):

- `web/src/api/blast.ts` — `BlastJobSummary` gains optional
  `stale?`, `refresh_blocked_reason?`, `cluster_power_state?`.
- `web/src/pages/BlastJobs/JobRow.tsx` — renders the frozen / no-cluster
  indicator when `job.stale` is set.

No IaC change. No new Azure surface — reuses the existing cached
`ManagedClusters.get` ARM probe.

## Validation evidence

- `uv run pytest -q api/tests/test_local_to_blast_job.py` → **24 passed**
  (6 new unit tests for `_blocked_refresh_reasons` + stale marking).
- `uv run pytest -q api/tests/test_external_blast_api.py::test_canonical_jobs_list_marks_running_row_stale_when_cluster_stopped`
  → **1 passed** (end-to-end route test: a `running` row on a stopped cluster
  returns `stale=True`, `refresh_blocked_reason="cluster_stopped"`,
  `cluster_power_state="Stopped"`, and the K8s refresh is not called).
- `uv run pytest -q api/tests` → **2377 passed, 3 skipped**
  (one unrelated pre-existing flaky `test_terminal_exec` timeout under parallel
  load; passes in isolation).
- `uv run ruff check` on all touched backend files → clean.
- `cd web && npm run build` → built in ~11 s; `npm test -- --run` →
  **462 passed**.
