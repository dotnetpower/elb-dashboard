# audit_log — collapse N+1 history query into a single bulk read

## Motivation
`audit_log` did:

1. `list_for_owner(limit=50)` → 1 Table query.
2. For each of (up to) 20 jobs: `get_history(job_id, limit=20)` → 20
   sequential Table round-trips.

Total: 21 round-trips per audit page render. Each refresh of the audit
view (or the SPA's polling tab) re-paid that latency, even when the
caller had no new events.

## User-facing change
None. Same row schema, same sanitisation, same per-job cap. The audit
panel renders the same content with a single OData query instead of
twenty.

## API / IaC diff
* `api/services/state_repo.py`
  * New `JobStateRepository.get_history_for_jobs(job_ids,
    per_job_limit=20)` issues one `query_entities(
    "PartitionKey eq 'a' or PartitionKey eq 'b' or …",
    results_per_page=per_job_limit * len(job_ids))` then groups locally
    and caps each bucket at `per_job_limit` so a chatty job cannot
    crowd out the others.
* `api/routes/audit.py::audit_log`
  * Replaces the inner `for job in jobs[:20]: get_history(job.job_id)`
    loop with a single `get_history_for_jobs([...])` call.
* `api/tests/test_smoke.py::test_audit_log_payload_is_sanitised`
  * `FakeRepo` updated to provide both `get_history` (back-compat) and
    the new `get_history_for_jobs` so the SPA contract test exercises
    the new path.

## Validation
* `uv run pytest -q api/tests -k audit` — 46 passed (audit + smoke +
  state_repo coverage).
* `uv run ruff check api/services/state_repo.py api/routes/audit.py` —
  clean.
