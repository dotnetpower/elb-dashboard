# BLAST jobs shared visibility (dev-stage flag)

## Motivation

During the development stage the Recent searches page only listed jobs whose
`owner_oid` matched the signed-in caller, so jobs submitted directly through the
OpenAPI surface (which carry an empty / different `owner_oid`) showed up while a
researcher's own UI-submitted jobs from a different identity context did not.
For single-tenant development we want every authenticated caller to see and open
every job regardless of who submitted it.

## User-facing change

- New environment flag `BLAST_JOBS_SHARED_VISIBILITY` (default `false`).
- When `true`, the BLAST job list (`GET /api/blast/jobs`), the operations job
  monitor (`GET /api/monitor/jobs`), and every per-job read/lifecycle route show
  and allow access to **all** jobs, not just the caller's own. Split-job child
  rollups keep working by grouping parents by their stored `owner_oid`.
- When `false` (production default) the per-owner isolation boundary is
  unchanged: a foreign job returns `403 not owner`.

The route layer still requires `require_caller` either way — this flag only
relaxes the per-row owner comparison, never the authentication gate.

## API / IaC diff summary

- `api/services/blast/job_state.py`: added `blast_shared_visibility_enabled()`
  and `_assert_job_owner(owner_oid, caller)`; routed the existing
  `_ensure_job_read_allowed` owner comparison through `_assert_job_owner`.
- `api/services/state/repository.py`: added owner-agnostic
  `JobStateRepository.list_all(*, limit, include_payload)` (queries
  `status ne 'deleted'`, newest-first).
- `api/routes/blast/jobs.py`: list route selects `list_all` when the flag is on
  and the repo supports it; cache key now includes `shared_visibility`; all
  inline owner comparisons replaced by `_assert_job_owner`; split-child rollup
  grouped by owner under shared visibility.
- `api/routes/blast/logs.py`, `api/routes/monitor/jobs.py`: inline owner checks
  replaced by `_assert_job_owner`; monitor list uses `list_all` under the flag.
- `api/routes/_blast_shared.py`: re-exports the two new helpers.
- `infra/modules/containerAppControl.bicep`: api sidecar gains
  `BLAST_JOBS_SHARED_VISIBILITY` env entry, default `'false'`.

## Validation evidence

- `uv run ruff check api` — all checks passed.
- `uv run pytest -q api/tests` — 2563 passed, 3 skipped.
- New tests in `api/tests/test_blast_jobs_routes.py`:
  - `test_assert_job_owner_isolation_default` (flag off → 403 for foreign owner,
    allowed for empty owner and self).
  - `test_assert_job_owner_relaxed_when_flag_on` (flag on → no raise).
  - `test_job_detail_blocks_other_owner_when_flag_off` (route returns 403).
  - `test_job_detail_allows_other_owner_when_flag_on` (route returns 200).
