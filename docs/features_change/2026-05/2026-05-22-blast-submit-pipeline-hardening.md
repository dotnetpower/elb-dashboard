# 2026-05-22 — BLAST submit pipeline hardening (live logs, ANSI, error banner, missing re-export)

## Motivation

Live monitoring of a dashboard-driven `elastic-blast submit` (job
`04d04496-…-3b0d73`) surfaced five defects that all manifested in the same
run:

1. The Submit Job step stalled with a single "Starting elastic-blast submit
   helper job…" line for ~33 s, then dumped five lines at once right before
   the task crashed.
2. The job ended in `failed` after 1 m 45 s with `error_code = "module
   'api.tasks.blast' has no attribute '_tail_text'"`, even though
   `elastic-blast submit` itself had streamed its output successfully.
3. The dashboard "Job Failed at Submit Job" banner showed
   `←[33m[parallel-prep] running 4 azcopy checks concurrently←[0m` — a benign
   helper log line wrapped in raw ANSI colour codes — instead of the real
   AttributeError above.
4. After the parent row was reconciled to `failed`, the Execution Steps
   timeline kept spinning on "Submit Job · 4/5 · Uploading workfiles" for
   12+ minutes. The crashed worker had left `output.steps.submitting.status =
   "running"` and the reconcile beat task wrote `status="failed"` through
   `repo.update(...)` directly, bypassing the payload merge that would have
   demoted the orphan step.
5. Worker chatter included Azure SDK INFO HTTP request/response dumps and
   ~1 Hz `AzureCliCredential.get_token_info succeeded` lines. That is noise
   only and not addressed in this change; tracked separately.

## User-facing change

* Submit Job step now streams elastic-blast output line-by-line in
  near-real-time (subject to the existing 15 s state-write debounce). Long
  silent gaps caused by stdout block-buffering in the Python child are gone.
* The failure banner shows the authoritative orchestrator error
  (`job.error` / `output.error`) first, so a Celery task crash is no longer
  hidden behind the last benign helper log line.
* ANSI colour codes (`\x1b[33m`, `\x1b[0m`, cursor / erase sequences, …) are
  stripped before any state, log artefact, or UI surface sees them.
* A submit that previously crashed in the post-stream tail snapshot now
  completes through to the K8s status refresh / artefact gate. The
  AttributeError class is also caught by a static cross-reference test so
  the same kind of re-export gap fails fast in CI instead of in production.
* The Execution Steps timeline stops spinning the moment the parent row is
  marked `failed`: orphan `running` step entries are demoted to `failed`
  (backend payload merge) and, defensively, the frontend resolves the
  failure phase before checking `step.status === "running"`.

## API / IaC diff summary

No HTTP route, Bicep, or Celery task name changed.

### `api/tasks/blast/__init__.py` (Fix A)

Re-export `_tail_text` from `api.tasks.blast.progress` so
`submit_task.py`'s post-stream `_blast._tail_text(...)` call resolves.

### `terminal/exec_server.py` (Fix B)

`_child_env()` now sets `env.setdefault("PYTHONUNBUFFERED", "1")`. With
`Popen(bufsize=0)` alone, the child Python (elastic-blast) still
block-buffered stdout in 8 KB chunks when stdout was a pipe; the new env
flag forces line-by-line flush so the NDJSON `/exec/stream` response sees
each line as it is printed.

### `api/services/sanitise.py` (Fix C)

Added `_ANSI_CSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")` and apply
it before the existing secret-masking passes. Removes colour, cursor, and
erase CSI sequences from any string headed to JSON state, append-blob log
artefacts, or the dashboard.

### `web/src/components/BlastStepTimeline/predicates.ts` (Fix D)

Re-ordered `getFailureText` candidates so authoritative error fields
(`job.error`, `output.error`, `output.message`, `customStatus.error`,
`customStatus.message`) are checked before per-step `error` / `output` /
`last_output`. Previously a populated `step.last_output` masked a real
`job.error`.

### `api/tasks/blast/progress.py` (Fix F)

`_merge_progress_payload` now sweeps every other step entry whose `status`
is `running` and demotes it to `failed` (with `success=False`,
`source="orphan_inferred"`, `completed_at=now`, propagated `error`) whenever
the parent merge writes `status="failed"`. Mirror of the existing
`status != "failed"` upgrade sweep that promotes earlier running steps to
`completed`.

### `api/tasks/blast/reconcile_task.py` (Fix G)

The `FAILURE`/`REVOKED` and `worker_lost` branches now go through
`_blast._update_state(...)` instead of `repo.update(...)` directly. The
direct update wrote top-level `status="failed"` but never re-ran
`_merge_progress_payload`, which is exactly what left the orphan
`submitting` step spinning in the timeline.

### `web/src/components/BlastStepTimeline/stepState.ts` (Fix E — defensive)

`getTimelineStepState` now checks `FAILURE_PHASES.has(phase)` before
`isStepRunning(step)`. A step left in `status: "running"` while the parent
is in a failure phase resolves to `error` / `done` / `skipped` instead of
`active`, so even if the backend payload merge is bypassed (legacy rows,
external writers, future regressions) the UI cannot render an infinite
spinner.

### `web/src/components/BlastStepTimeline/StepLogSection.tsx` (Fix H — dedup)

`buildStepLog` for `submitting` embeds the JobState snapshot under
`--- Live Console Output ---`. `StepLogSection` also subscribes to the SSE
event bus and used to append a second `--- Live Stream ---` block, so the
same lines rendered twice (the snapshot is just a debounced sample of the
same stream). New `stripConsoleOutputBlock(log)` helper removes the
snapshot block when SSE has events for the step, then a single
`--- Live Console Output ---` block is appended from the live stream.

### `web/src/components/BlastStepTimeline/constants.ts` (Fix I — `submitted` mapping)

`PHASE_TO_STEP` now maps `submitted → "running"`. `submitted` is the
transit phase between submit completion and the first `poll_running_status`
tick reporting pods=Running (10-30 s). Without the mapping, every step
resolved to `pending` and the timeline showed no active spinner during the
gap, looking dead even though the worker was waiting on K8s. Added a
matching `PHASE_MESSAGES["submitted"]` so the BLAST Run step shows
"Job accepted by AKS. Waiting for pods to start running..." instead of the
default placeholder.

### `scripts/dev/local-run.sh` (Fix J — reap stale celery workers/beat)

Live monitoring caught the same `_tail_text` AttributeError reappearing
after the original Fix A was already applied. Root cause: orphaned celery
worker processes from earlier `worker: start` runs (the supervisor was
killed, but the prefork master + forks were reparented to PID 1 and kept
consuming from Redis with stale, pre-fix code). New submits landed
randomly on whichever worker was first, so the bug appeared intermittent.
The `worker)` and `beat)` branches now `pkill -TERM` / `pkill -KILL` any
matching `python3 -m celery -A api.celery_app:celery_app worker` and
`celery -A api.celery_app beat` processes before `exec`-ing the wrapper —
mirroring the `api)` branch's port-lock guard. Without this, every
`worker: start` invocation could silently add a duplicate worker.

### `web/src/components/BlastStepTimeline/buildStepLog.ts` (Fix K — misleading polls text)

The `running` `done` branch previously rendered
`✓ BLAST completed after ${polls ?? "?"} polls (~${(polls ?? 0) * 30}s).`,
producing the literal text "after ? polls (~0s)" for short runs whose
state row never carried a `polls` field. Now the elapsed value comes from
the closed step's `duration_ms` (always present) and the polls phrase is
omitted unless `polls` is a positive number. The `exporting_results`
`done` branch also dropped its "(no output)" placeholder so empty export
logs no longer look like a missing capture.

### `web/src/components/BlastStepTimeline/StepLogSection.tsx` (Fix L — open by default)

`isOpen = expanded[step.key] ?? (state === "active" || state === "error")`
keeps only the currently-active or failed step expanded by default. An
earlier iteration auto-expanded every non-pending step (so completed
runs landed with every step open), but the user found that too busy;
collapsed-by-default for done/skipped steps is the canonical look, with
the user toggling individual steps as needed.

### `web/src/pages/BlastResults.tsx` (Fix L2 — auto-switch to Descriptions on completion)

A `previousPhaseRef` watches `effectivePhase`; on the rising edge of
`completed` while the user is still on `tab=run` (typically because the
page auto-routed them there during submit), the URL is rewritten to
`tab=descriptions` so the analytics view shows up the moment results
are ready. The transition only fires once per phase change, so a user
manually navigating back to `run` on an already-completed job is NOT
flipped away again.

### `web/src/hooks/useStickToBottom.ts` + `web/src/pages/blastResults/ExecutionStepsCard.tsx` (Fix M — auto follow tail)

New window-scope `useStickToBottom({ version, enabled })` hook
implements the "follow the tail" pattern: scroll to the bottom on mount
and on every `version` change while the user remains anchored within
96 px of the bottom; pause auto-scroll once the user manually scrolls
up; re-arm when they scroll back to the bottom. `ExecutionStepsCard`
composes the version from `(phase, updated_at, submitting.log_line_count)`
so every new log line or step transition triggers a scroll. The user
lands on the latest output without manually scrolling and stays glued to
new output as the run progresses.

### `web/src/pages/blastResults/useBlastResultsState.ts` (Fix N — terminal backfill polling)

`refetchInterval` used to return `false` the moment the job entered any
terminal phase. But the reconcile beat writes trailing artefacts —
notably the K8s pod log tail into `running.last_output` — AFTER
`phase=completed`, so the dashboard stayed pinned to the partial
pre-reconcile snapshot (the "BLAST completed after ? polls" placeholder
without pod logs). Now `refetchInterval` returns
`TERMINAL_BACKFILL_POLL_INTERVAL_MS = 10s` until `updated_at` ages
beyond `TERMINAL_BACKFILL_WINDOW_MS = 5 min`, at which point polling
stops entirely. Combined with Fix K, completed runs now show their full
K8s pod log tail. Both `jobQuery` and `executionStepsQuery` now also set
`refetchOnWindowFocus: true` + `refetchOnMount: "always"` so a user
returning to the page hours later still pulls fresh state instead of the
tail-end cached snapshot.

### `api/routes/blast/jobs.py` (Fix P1 — execution-steps endpoint prefers live state)

The `/api/blast/jobs/{id}/execution-steps` route used to return the
persisted snapshot blob first and fall back to live state only when the
blob was missing. The snapshot is written ONCE by
`finalize_job_artifacts` at the moment the job reaches a terminal phase;
trailing artefacts (K8s pod log tails on `running.last_output`, …) can
be backfilled later but the snapshot is NEVER re-written automatically.
That made the dashboard show stale "BLAST completed." with no pod logs
even though `running.last_output` had the tail. Now the endpoint:
prefers live Table state, falls back to the persisted blob only if the
Table read raises, and 404s if both are unavailable. Locked by
`test_blast_execution_steps_route.py` (3 tests).

### `api/tasks/blast_artifacts.py` (Fix P3 — self-retry on empty pod log capture)

`finalize_job_artifacts` previously upserted `artifact_state="ready"`
even when `persist_completed_job_pod_logs` returned `{}` — leaving a
frozen snapshot with no pod logs forever, because
`artifact_build_should_enqueue` then refuses to re-enqueue the task. The
function now accepts a `pod_log_attempt` kwarg (defaults to 1) and, when
the persistence returned empty, schedules a delayed self-retry via
`apply_async(countdown=60)` up to `_POD_LOG_RETRY_MAX = 3` attempts.
K8s pods often haven't finished flushing stdout when the job container
exits; the delayed retries give them 60-180 s to flush their tail
before the artefact bundle is considered final.

## Validation evidence

```text
uv run pytest -q api/tests
  943 passed in 27.65s

uv run pytest -q api/tests/test_sanitise.py api/tests/test_terminal_exec.py \
                 api/tests/test_blast_tasks.py
  140 passed in 13.69s

cd web && npm test -- predicates
  ✓ src/components/BlastStepTimeline/predicates.test.ts (4 tests) 2ms
  Test Files  1 passed (1)
       Tests  4 passed (4)

cd web && npm run build
  ✓ built in 7.05s

uv run ruff check api/tasks/blast/__init__.py api/services/sanitise.py \
                  terminal/exec_server.py \
                  api/tests/test_sanitise.py api/tests/test_terminal_exec.py \
                  api/tests/test_blast_tasks.py
  All checks passed!
```

### New regression tests

| Test | File | Guards |
|------|------|--------|
| `test_submit_task_helpers_are_reexported_on_blast_package` | `api/tests/test_blast_tasks.py` | Cross-reference every `_blast.X` name used by submit_task and friends against `api.tasks.blast.__all__` / `hasattr` so a missing re-export fails at import-time instead of mid-submit. |
| `test_merge_progress_payload_demotes_orphan_running_steps_on_failed_update` | `api/tests/test_blast_tasks.py` | Lock the orphan-`running` sweep on `status="failed"` merges. |
| `test_strips_ansi_csi_color_codes`, `test_strips_ansi_with_cursor_and_erase_sequences` | `api/tests/test_sanitise.py` | Lock ANSI stripping into the sanitise contract. |
| `test_child_env_forces_pythonunbuffered` | `api/tests/test_terminal_exec.py` | Stub `az` that echoes `PYTHONUNBUFFERED` and assert the spawned subprocess sees `=1`. |
| `getFailureText prefers job.error` (+ 3 siblings) | `web/src/components/BlastStepTimeline/predicates.test.ts` | Lock the new candidate ordering. |
| `never renders a spinner when the parent job is in a failure phase` | `web/src/components/BlastStepTimeline/stepState.test.ts` | Lock that an orphan `submitting.status="running"` no longer produces `active` under `phase="failed"`. |
| `activates BLAST Run while phase is the transit submitted` | `web/src/components/BlastStepTimeline/stepState.test.ts` | Lock `PHASE_TO_STEP["submitted"] = "running"` so the timeline does not sit silent for 10-30 s after submit. |
| `stripConsoleOutputBlock` (+ 3 siblings) | `web/src/components/BlastStepTimeline/StepLogSection.test.ts` | Lock the SSE / snapshot de-duplication. |

### Manual evidence captured during the failing run

* Job state snapshot (`GET /api/blast/jobs/04d04496-…`) immediately after
  the crash showed:
  * `status="failed"`, `error="module 'api.tasks.blast' has no attribute
    '_tail_text'"`,
  * `output.steps.submitting.last_output` containing raw `\x1b[33m…\x1b[0m`
    codes — exactly the string the UI rendered as the failure message.
* `submitting.started_at=17:28:04`, `updated_at=17:29:01` (one state write
  in 57 s) with `log_line_count=5` → confirms the pipe-buffering symptom
  Fix B targets.
* `.logs/monitor/run-022727Z.log` (kept locally, not checked in) captured
  the full sequence including
  `AttributeError: module 'api.tasks.blast' has no attribute '_tail_text'`
  in the worker traceback.
