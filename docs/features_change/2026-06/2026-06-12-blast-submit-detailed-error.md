# BLAST submit failures now surface the detailed console error

## Motivation

When a BLAST submit failed, the Run details page often showed only
"Job Failed at Submit Job" with "No detailed error was recorded by the
orchestrator." and an empty Submit step, making the failure impossible to
diagnose from the browser. Two backend gaps caused this:

1. `_result_error` returned an empty string when the sibling emitted a
   `{"kind": "error"}` payload with a missing/empty `message`, so the
   `error_code` written to the job row was blank.
2. The non-retryable `submit_failed` path persisted only the short
   `error_code` tail and never wrote the full stdout/stderr to the
   `submitting` step, so the SPA had nothing detailed to render.

## User-facing change

- A failed BLAST submit now always records a non-empty, actionable error
  (structured message → raw stderr/stdout tail → exit-code / timeout
  diagnostic), so "No detailed error was recorded" no longer appears for a
  genuine submit failure.
- The full submit console output (up to `LIVE_OUTPUT_SNIPPET_CHARS`) is
  persisted on the failed Submit step, so the Run details page shows the
  complete stdout/stderr — including the underlying `elastic-blast` error —
  instead of a truncated tail.

## API / IaC diff summary

- `api/tasks/blast/cli_parsing.py` — `_result_error` only trusts a non-empty
  structured message, then falls back to raw stream output, then to an
  exit-code / timeout diagnostic. Never returns an empty string.
- `api/tasks/blast/submit_task.py` — the `submit_failed` terminal update now
  also writes `output`, `last_output`, `exit_code`, `log_line_count`,
  `terminal_duration_ms`, and `timed_out` so the failed step carries the full
  console output.
- No IaC change.

## Validation

- `uv run pytest -q api/tests/test_blast_tasks.py api/tests/test_blast_submit_capacity_gate.py`
  — 154 passed, including new `_result_error` regression cases and
  `test_submit_failed_persists_full_console_output`.
- `uv run pytest -q api/tests/test_local_to_blast_job.py api/tests/test_job_artifacts.py`
  — 52 passed (response projection + artifact finalize consumers).
- `uv run ruff check` on the touched modules and tests — clean.
