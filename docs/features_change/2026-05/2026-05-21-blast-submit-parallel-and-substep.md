# BLAST submit: parallelise prep + sub-progress badge

## Motivation

Timing measurement of job `b835e386-4ae8-4c28-b4d3-518ce0aec11e` showed that the
`submitting` phase accounted for **134 s out of the total ~200 s** wall time
even though the actual `blastn` containers ran in roughly 17 s. The `submitting`
phase wraps multiple sequential `azcopy` / k8s round-trips inside the sibling
`elastic-blast` CLI plus a number of *prep* calls (`warmup_ready_for_submit`,
terminal `az login`, two oracle queries) that the dashboard issued one after
another before launching the CLI. From the operator's perspective the phase
also looked completely opaque — a single yellow line for ~2 minutes with no
indication of where time was being spent.

## User-facing change

1. **Sub-progress badge inside the `Submit Job` row.** When the streaming
   `elastic-blast submit` output crosses one of the five existing yellow
   progress markers, the UI shows a small "N/5 · &lt;label&gt;" badge next to
   the step row description (active state only). The current label sequence:
   `Writing configuration` → `Analysing query mode` → `Splitting queries`
   → `Uploading workfiles` → `Submitting K8s jobs`. The badge is unobtrusive
   (warning-toned text on a soft background, no animation) and disappears
   once the step transitions to `done` / `error`.
2. **Parallelised submit prep.** The dashboard now runs the four
   independent prerequisite calls (`warmup_ready_for_submit`, terminal
   `az login`, `tie_order_oracle`, `db_order_oracle`) in a
   `ThreadPoolExecutor(max_workers=4)` while still gating the actual CLI
   submit on the warmup result. Expected saving: ~10-14 s of wall time on
   the `submitting` phase entry, with the bulk of the 134 s coming from the
   sibling CLI itself (unchanged in this PR).

## API / IaC diff summary

No public HTTP API changes. The job state payload exposed under
`payload._progress.steps.submitting` now carries an optional
`submit_progress = {"index": int, "total": int, "label": str}` field, and the
allow-list inside `api/tasks/blast/progress.py::_compact_progress_details`
includes the new key so it survives compaction.

No IaC or sibling repo (`dotnetpower/elastic-blast-azure`) changes — the
implementation reads existing CLI log lines and reorders calls on the
dashboard side only.

## Validation evidence

- `uv run pytest -q api/tests/test_blast_tasks.py` → `118 passed`
- `uv run pytest -q api/tests` → `868 passed`
- `uv run ruff check api` → `All checks passed!`
- `cd web && npm run build` → `built in 9.70 s` (no TS errors)
- New unit tests added in `api/tests/test_blast_tasks.py`:
  - `test_detect_submit_substep_matches_yellow_progress_markers`
  - `test_detect_submit_substep_returns_none_for_unrelated_lines`
  - `test_stream_submit_command_emits_submit_progress_state_update`
  - `test_merge_progress_payload_keeps_submit_progress_field`

Direct end-to-end BLAST run was deferred: a fresh `submit` against
`elb-dev-rg` costs ~3 minutes of AKS time + ACR egress per cycle, and the user
was away. The unit + build coverage above pins each behaviour we changed:

- The five marker regexes match the actual sibling-repo output strings
  (verified against `~/dev/elastic-blast-azure/src/elastic_blast/commands/submit.py`
  and `azure.py` source lines).
- The state-update path receives `submit_progress=...` and a forced flush
  on every detected marker (bypassing the normal 15 s coalesce window).
- The UI badge resolves from `stepsData[step.key].submit_progress` and only
  renders for `state === "active"`.

## Files touched

- `api/tasks/blast/__init__.py` — added `SUBMIT_SUBSTEP_PATTERNS`,
  `_detect_submit_substep`, substep flushing inside `_stream_submit_command`,
  and the `ThreadPoolExecutor` block in `submit()`.
- `api/tasks/blast/progress.py` — allow-list `submit_progress` in
  `_compact_progress_details`.
- `api/tests/test_blast_tasks.py` — four new unit tests.
- `web/src/components/BlastStepTimeline/StepRow.tsx` — new `subProgress` prop +
  badge rendering.
- `web/src/components/BlastStepTimeline/StepLogSection.tsx` — read
  `stepsData[step.key].submit_progress` and pass to `StepRow`.

## Not changed (deliberately)

- Sibling repo `~/dev/elastic-blast-azure` is untouched. The actual 134 s
  `submitting` cost lives inside the CLI's sequential `azcopy` forks; that
  is a separate change with a wider blast radius and will be addressed in a
  follow-up PR if/when the dashboard-side savings are insufficient.
- No new background task or Celery chain. Parallelism stays in-process via
  `ThreadPoolExecutor` to keep task semantics simple.
