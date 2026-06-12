# External BLAST job failure detail recovered from cluster artifacts

## Motivation

When an external (OpenAPI / `/v1/jobs`) BLAST job failed, the dashboard Run
details banner often showed only:

> External BLAST job failed, but the OpenAPI service reported no error detail.
> Check the sibling job logs for the underlying cause.

The sibling OpenAPI service only reports a coarse lifecycle and stamps generic
strings on failure (`one or more BLAST jobs failed`, `BLAST job failed`, `submit
job failed before creating BLAST jobs`) — or nothing at all when it detects a
`metadata/FAILURE.txt` marker. The authoritative blastn diagnostics
(`metadata/FAILURE.txt` stderr + `logs/BLAST_RUNTIME-NNN.out` exit code) live in
the workload results container, which the dashboard already reads for its own
Celery-submitted jobs but did not consult for external-origin jobs.

## User-facing change

On the **Run details** page (detail view) of a failed external job, the banner
and the failed-step error now show the real cluster-side cause — e.g.
`BLAST search exited with code 2: <blastn stderr head>` — instead of the
"no error detail" placeholder, whenever the results container has the runner's
failure artifacts. List rendering is unchanged (the Storage read is gated to the
detail view so the jobs list never pays for it), and a genuinely specific
sibling error is left untouched.

No sibling redeploy is required — this is a dashboard-side enrichment over the
existing results-container artifacts.

## API / code diff summary

- New focused module `api/services/blast/runtime_failure.py` exposing
  `read_blast_runtime_failure(storage_account, job_id)` — the best-effort
  `FAILURE.txt` / `BLAST_RUNTIME` reader, moved verbatim from
  `job_state._read_blast_runtime_failure` so both the dashboard K8s-refresh
  path and the external projection can share it without a layering cycle. The
  captured `FAILURE.txt` stderr is now redacted via `sanitise` **at the source**
  (closes a pre-existing gap: the K8s-refresh path previously fed unsanitised
  stderr into the step error — a SAS/Bearer/GUID in an azcopy diagnostic could
  have leaked to the UI). Both callers now surface secret-free text.
- `api/services/blast/job_state.py`: re-exports the reader under the historical
  private name `_read_blast_runtime_failure` (K8s-refresh caller + existing
  tests/monkeypatches keep working); the `_local_to_blast_job` external-origin
  branch now enriches a generic/empty failure error from the results container
  (keyed by the sibling openapi job id), gated to `include_database_metadata`.
- `api/services/blast/external_job_projection.py`: new
  `_enrich_external_failure_detail(...)` helper + `_EXTERNAL_GENERIC_FAILURE_MESSAGES`;
  `_external_to_blast_job` calls it on the detail-view path. The recovered
  detail is sanitised + clamped (`_clamp_error_message`) before display
  (Charter §12).
- No response-shape change: only the `error` / `output.error` /
  `output.steps[failed].error` string content is enriched at runtime.

## Validation

- `uv run pytest -q api/tests/test_external_job_projection.py
  api/tests/test_local_to_blast_job.py` — new cases:
  - `test_external_failed_job_enriched_with_cluster_detail`
  - `test_external_failed_enrichment_skipped_on_list_view`
  - `test_external_failed_enrichment_preserves_specific_error`
  - `test_local_to_blast_job_external_failed_row_enriched_with_cluster_detail`
  - `test_local_to_blast_job_external_enrichment_skipped_on_list_view`
  - `test_read_blast_runtime_failure_redacts_secrets_in_stderr` (sanitise at source)
- `uv run ruff check api` — clean.
- `uv run pytest -q api/tests` — 3339 passed, 3 skipped (no regressions from the
  reader-module move).

## Follow-up (out of scope, requires sibling work + redeploy)

The root-cause-complete fix is in the sibling `elastic-blast-azure`
(`docker-openapi/app/main.py` `_refresh_job_status`): embed the real pod reason
(`_k8s_pod_stuck_reason`) + `FAILURE.txt` content in its own `error` field on
the `marker == "failed"` / `failed_terminal` / `submit_failed_terminal` paths,
so even non-dashboard API callers get actionable detail. That needs an OpenAPI
image rebuild/redeploy and is tracked separately.
