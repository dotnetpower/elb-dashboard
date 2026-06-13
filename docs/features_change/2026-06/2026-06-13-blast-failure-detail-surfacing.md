---
title: BLAST failures surface the real reason, not just an opaque error code
description: Retry-exhausted BLAST submit failures now show the underlying exception detail on the Run details page instead of a bare machine error code.
tags:
  - blast
  - ui
---

# BLAST failures surface the real reason, not just an opaque error code

## Motivation

Follow-up to the "no output captured" investigation. While auditing the
end-to-end error path the user asked: *does the dashboard reliably record the
detailed error, and can it be shown on screen?* Most failures could not be
confirmed because they only displayed a short machine code.

## Root cause

`api/tasks/blast/state.py::_retry_or_fail` (the path for
`terminal_exec_unavailable`, `terminal_az_login_failed`,
`terminal_kubeconfig_failed`, `blast_submit_requeue_failed`,
`submit_retryable_failure`, and the capacity-gate denials) persisted **two**
things on the final failure:

- a short machine `error_code` (the classification), and
- a human-readable `error` detail (the actual exception text).

But the detail was silently lost:

1. `error` was **missing from the `_compact_progress_details` allow-list** in
   `api/tasks/blast/progress.py`, so the detail was dropped before it reached
   the payload.
2. The merge then ran `step["error"] = error_code`, **clobbering** any detail
   with the bare code.

The Run details page reads `job.error` first, which resolved to the machine
`error_code`, so the operator saw `terminal_az_login_failed` with no indication
of *why* the az login failed. The detail only survived in the append-only
history blob, which the page does not surface.

(The non-retryable `submit_failed` path was unaffected — it already stores the
full text as `error_code`. The K8s-runtime and external-OpenAPI failure paths
were already detailed by earlier fixes.)

## User-facing change

- A retry-exhausted BLAST failure now shows `「code」: 「detail」` on the Run
  details banner — e.g. `terminal_az_login_failed: az login --identity failed:
  ManagedIdentityCredential authentication unavailable…` — so the classification
  *and* the underlying reason are visible.
- When a failure has only a human detail and no machine code, the detail is
  shown rather than nothing.
- A transient failure detail is dropped once the job completes, so it cannot
  linger as a red banner on a job that later succeeds (same stale-error class as
  the earlier `worker_lost` suppression).

## API / IaC diff summary

- `api/tasks/blast/progress.py`
  - `_compact_progress_details`: add `error` to the allow-list.
  - `_merge_progress_payload`: keep the human detail as the step `error`, stash
    the machine code under `step.error_code` instead of overwriting, and mirror
    the detail to a top-level `payload.error` (popped on completion).
- `api/services/blast/job_state.py`
  - `_job_error_for_response`: return `「error_code」: 「detail」` when both exist
    and differ; return the bare detail when there is no code; the
    completed-suppression guard runs first so a successful job never shows an
    error. The raw `error_code` field on the response is unchanged for any
    frontend logic.

No infra change. `out["error_code"]` (raw machine code) is unchanged; only the
human-facing `error` string is enriched.

## Validation evidence

- New tests:
  - `test_merge_progress_payload_keeps_human_detail_alongside_machine_code`
  - `test_merge_progress_payload_drops_top_level_error_on_completion`
  - `test_local_to_blast_job_combines_machine_code_with_human_detail`
  - `test_local_to_blast_job_surfaces_human_detail_when_no_machine_code`
- `uv run pytest -q api/tests` → 3500 passed, 3 skipped.
- `uv run ruff check api` → clean.
- Frontend `blastResultsModel.test.ts` + `predicates.test.ts` → 14 passed (the
  display chain consumes `job.error` first, so the enriched string flows through
  unchanged).
