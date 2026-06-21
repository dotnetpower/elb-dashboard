# Serve external/Service Bus result downloads from Storage after cluster auto-stop

**Date:** 2026-06-21

Closes the proxy-only download gap tracked in
[#61](https://github.com/dotnetpower/elb-dashboard/issues/61).

## Motivation

A Service Bus completion event's `download_url` (and the Results "download"
button) stopped working once the AKS cluster auto-stopped: the external-job file
download proxied through the elb-openapi pod, which is gone when the cluster is
stopped, so a consumer following the `download_url` after the idle window got an
error. The result bytes are durably in Storage — only the *serving path* was
coupled to cluster uptime, defeating the "complete and download later" model for
external/Service Bus consumers.

## User-facing change

- A completed external/Service Bus job's result file now downloads **even after
  the cluster auto-stops**: when the openapi proxy is unreachable the `api`
  sidecar streams the result blob straight from the trusted workload Storage
  account via the managed identity (never a SAS — charter §9).
- Jobs that completed **before** this change have no stored manifest, so their
  offline download returns a clear `404 result_unavailable_offline` instead of a
  confusing error; they still download normally while the cluster is up.

## Code change summary

- `api/services/blast/external_job_projection.py` — `_external_result_files` now
  carries the sibling's `blob_path` (relative to `results/{job_id}/`).
- `api/services/state/job_state.py` + `api/services/state/repository.py` —
  `JobState` gains a durable `result_manifest` JSON column (`[{file_id,
  blob_path}]`); `update()` accepts it via a MERGE patch (no canonical recompute).
- `api/tasks/servicebus/tasks.py` — `_persist_result_manifest` captures the
  `file_id → blob_path` manifest into the column at the succeeded transition
  (cluster up, the openapi detail with `result.files[].blob_path` in hand).
  Best-effort: a failure never blocks the completion event.
- `api/services/external_blast.py` — `stream_result_file_from_storage` resolves
  `file_id → blob_path` from the manifest and streams
  `results/{job_id}/{blob_path}` from the job's `storage_account` via the shared
  `stream_blob_bytes` helper (path-traversal validated, §9 concurrency gated).
- `api/routes/elastic_blast.py` — the download route falls back to the Storage
  helper only when `stream_file` raises the `openapi_unreachable` 503; every
  other error (incl. the fallback's own offline 404) propagates unchanged.

The blob path format was confirmed from the sibling
(`elastic-blast-azure docker-openapi/app/main.py`): `results_url =
{account}/results/{job_id}`, `azcopy ls` yields paths relative to it, and the
sibling's own download uses `{results_url}/{blob_path}`.

No IaC change. The new Table column is additive and schemaless.

## Validation

- `uv run pytest -q api/tests` — 4156 passed, 3 skipped. New tests:
  - `test_external_result_files_preserves_blob_path`
  - `test_job_state_round_trips_result_manifest_column`
  - `test_persist_result_manifest_writes_column` / `_noop_without_blob_paths`
  - `test_stream_result_file_from_storage_*` (manifest hit, no manifest, unknown
    file_id, no account)
  - `test_download_route_falls_back_to_storage_when_openapi_unreachable` /
    `_propagates_non_openapi_errors`
- Live (moonchoi, cluster stopped): the offline `404 result_unavailable_offline`
  path is verified against pre-feature job `9ca72c6092b0` on revision 0000632 —
  the route catches the openapi-down 503 (both `openapi_unreachable` and the
  `openapi_not_configured` variant a redeploy-while-stopped surfaces, which a
  first live run caught the fallback was missing) and the Storage fallback
  returns the honest offline 404 because that job predates the manifest. The
  success path (a real result byte-stream after auto-stop) activates for jobs
  that complete after this deploy and is covered by the unit tests above.
