# External-origin failed BLAST jobs surface the real sibling error

## Motivation

A live `core_nt` BLAST job submitted through the external OpenAPI plane
(`user="api"`) failed and the dashboard Run details showed only the generic
banner:

> External BLAST job failed, but the OpenAPI service reported no error detail.
> Check the sibling job logs for the underlying cause.

The sibling `elb-openapi` service actually had a precise, actionable cause —
queried directly on the pod:

```
GET /api/v1/elastic-blast/jobs/abda47568f08
"error": { "code": "BLAST_FAILED",
  "message": "... BLAST database .../core_nt/core_nt memory requirements exceed
  memory available on selected machine type \"Standard_E16s_v5\". Please select
  machine type with at least 251.7GB available memory." }
```

The dashboard could not surface it because the sibling's `/v1/jobs` **LIST**
snapshot (the only payload `_sync_external_jobs_to_table` consumes) carries no
`error` field — only the per-job **detail** endpoint
(`GET /api/v1/elastic-blast/jobs/{id}`, reached by `external_blast.get_job`)
does. A researcher hitting a failed external job therefore had to `kubectl exec`
into the sibling pod to learn why — a real observability gap.

## User-facing change

For an external-origin (`/v1/jobs`) BLAST job that **transitions to `failed`**,
the dashboard now recovers the real failure cause from the sibling detail
endpoint once and surfaces it in:

- **Recent searches** row error / **Run details** banner (top-level `error`).
- The **Execution Steps → Submit Job** step's inline error (`output.steps[...]`),
  replacing the generic "no error detail" placeholder.

No change for successful, running, or cancelled jobs.

## API / IaC diff summary

No API surface or IaC change. Two backend service edits:

- `api/services/blast/external_jobs.py`
  - New best-effort helper `_recover_external_failure_error(job_id,
    infrastructure)` calls `external_blast.get_job(...)` (resolving the
    per-cluster endpoint from the row's own subscription/RG/cluster) and returns
    the sanitised sibling error message. Never raises — a sibling outage / an
    unresolved endpoint degrades to `None`, preserving the generic banner, so
    error recovery can never turn a successful sync into a failure.
  - `_sync_external_jobs_to_table` calls it on the **failed transition**
    (`status_changed` to `failed`) and on a **new row first seen already
    failed**, persisting the message into the indexed `error_code` column.
    Guarded on an empty existing `error_code` so a stable failed row never
    re-fetches (once-only, idempotent, bounded by the 70 s sync cache).
- `api/services/blast/job_state.py`
  - `_local_to_blast_job` passes the persisted failed-row error (`response_error`)
    as `_external_step_projection(..., error_message=...)` so the recovered
    cause flows into the failed step's inline `error` / `output`, not just the
    banner. A genuinely specific snapshot error still wins when no column value
    is present (precedence: persisted column error → snapshot error → generic
    placeholder).

## Scope / known limitation

- The recovery fires on the failed **transition** only. Rows that were already
  in `failed` state before this change shipped are not back-filled (they keep
  the generic banner until they age out of Recent searches) — this is a
  deliberate tradeoff to keep the fetch strictly once-per-job and avoid a
  per-poll re-fetch for any job the sibling genuinely has no error for.
- The underlying job failure itself (`core_nt` on `Standard_E16s_v5`:
  251.7 GB required vs 128 GB nominal) is a correct elastic-blast memory-fit
  rejection of an under-sized machine type, surfaced via the external submit
  path which bypasses the dashboard's own `node_memory_fit` pre-flight gate.
  This change makes that cause visible; choosing an adequate machine type (or
  routing through the dashboard submit gate) remains the user action.

## Validation evidence

- Live root-cause confirmed against the deployed sibling pod
  (`elb-cluster-01`, job `abda47568f08`): `get_job` returns the
  `BLAST_FAILED` memory-fit message that the dashboard previously hid.
- New tests (all green):
  - `api/tests/test_external_blast_api.py`:
    `test_sync_external_failed_new_row_recovers_error_into_error_code`,
    `test_sync_external_failed_transition_recovers_error`,
    `test_sync_external_failed_transition_skips_recovery_when_error_code_present`,
    `test_sync_external_failed_recovery_never_breaks_sync_on_sibling_outage`.
  - `api/tests/test_local_to_blast_job.py`:
    `test_local_to_blast_job_external_failed_row_uses_persisted_error_code`
    (plus the existing `..._surfaces_error` placeholder test still green for the
    no-recovery path).
- `uv run ruff check api/services/blast/external_jobs.py
  api/services/blast/job_state.py` — clean.
- `uv run pytest -q api/tests/test_external_blast_api.py
  api/tests/test_local_to_blast_job.py` — 126 + new tests pass.
