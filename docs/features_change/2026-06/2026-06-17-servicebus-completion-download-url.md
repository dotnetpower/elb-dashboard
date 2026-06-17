# Service Bus completion events carry result-file download URLs

**Date:** 2026-06-17
**Area:** Service Bus integration (`api/tasks/servicebus`), examples (`example/servicebus`)

## Motivation

An external consumer of the optional `elastic-blast-completions` topic could see
that a job `succeeded` but had to enumerate the result files itself (parse
`result_ref.api`, call the job detail, read `result.files`, then build each file
URL) before it could download anything. The request was for the completion
message to carry a ready-to-use `download_url` per result file so a consumer can
fetch results directly.

## User-facing change

A **succeeded** `blast.transition` event now includes a `result_files` array:

```json
"result_files": [
  {
    "file_id": "merged_results.out.gz",
    "name": "merged_results.out.gz",
    "format": "blast_tabular",
    "size": 12345,
    "download_url": "https://<dashboard-host>/api/v1/elastic-blast/jobs/{job_id}/files/merged_results.out.gz"
  }
]
```

`download_url` points at the dashboard's **authenticated file-streaming gateway**
(`GET /api/v1/elastic-blast/jobs/{job_id}/files/{file_id}`), which the `api`
sidecar proxies to the sibling OpenAPI plane. A consumer downloads by calling it
with a bearer token. It is **never** a Storage SAS URL or a direct blob URL
(charter §9). When the dashboard public base cannot be resolved, the file
metadata is still emitted but `download_url` is omitted, so a subscriber can fall
back to `result_ref`.

The field is present only on `succeeded` events when the optional completion
topic is configured; `queued` / `running` / `failed` events are unchanged. The
addition does not alter the `event_id` dedup digest.

## API / IaC diff summary

- `api/tasks/servicebus/tasks.py`
  - new `_result_files_for_event(job, openapi_job_id)` — builds the per-file
    metadata + `download_url` from the sibling job detail
    (`_external_result_files`) and the resolved dashboard public base
    (`resolve_control_plane_url`). Capped at `_MAX_RESULT_FILES` (25).
  - `_transition_event(...)` gains an optional `result_files` parameter, emitted
    only when supplied.
  - `_publish_one_bridge(...)` attaches `result_files` on the `succeeded`
    transition (best-effort: a build failure logs and emits an empty list).
- No IaC change. No new env var. Uses the existing `CONTAINER_APP_NAME` +
  `CONTAINER_APP_ENV_DNS_SUFFIX` (or `DASHBOARD_PUBLIC_URL` / operator setting)
  that `resolve_control_plane_url` already reads.
- `example/servicebus/consume.py` — `--source completions --download` now reads
  `result_files`, acquires a bearer token (`ELB_BEARER_TOKEN` or
  `az account get-access-token --resource $ELB_API_CLIENT_ID`), and downloads
  each `download_url` to `--download-dir`.

## Validation

- `uv run pytest -q api/tests/test_servicebus_tasks.py` — 35 passed (includes the
  new `test_result_files_for_event_*`, `test_transition_event_includes_result_files_*`,
  and `test_publish_transitions_succeeded_attaches_download_urls`).
- `uv run ruff check api/tasks/servicebus/tasks.py api/tests/test_servicebus_tasks.py example/servicebus` — clean.
- `example/servicebus/consume.py --self-test` — green (asserts `plan_downloads`
  turns the event's `result_files` into download targets and that the URL is the
  dashboard gateway, not a SAS URL).
- Live end-to-end against `ca-elb-dashboard` + `elb-cluster-01`: send a real
  Service Bus request → drain → OpenAPI submit → BLAST run → succeeded
  completion event carrying `download_url` → consumer downloads the result file.
