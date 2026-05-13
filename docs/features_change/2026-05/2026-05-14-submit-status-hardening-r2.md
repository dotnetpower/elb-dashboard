# 2026-05-14 — Critical hardening pass on submit + status (round 2)

## Motivation

After the first hardening (`2026-05-14-job-submit-and-warmup-hardening.md`)
made `submit` reach the BLAST stage, a critical-review pass surfaced eight
more failure modes that could surface as **silent success** (worse than
loud failure) or could break correctness when two submits ran on the
same cluster. This change closes them all.

## P0 — silent success risks

### P0-1 / P0-2 — `EXIT_CODE` parser & log tail size

`_parse_exit_code` previously scanned forward and returned `6 (UNKNOWN)`
when no marker was found. The submit-helper's `tailLines: 120` could
truncate the marker if elastic-blast printed a long traceback before it,
producing `success=False` with no diagnostic. Now:

* Scan **from the end** so the marker survives long preceding tracebacks.
* Bump `tailLines` to 400 and the UI-facing slice to 8 000 bytes so the
  `_parse_exit_code` always operates on the full log.
* Log a WARNING with the byte count when the marker is missing — this
  lights up in App Insights so we can spot truncated jobs.

### P0-3 — concurrent submit cleanup race

`_cleanup_stale_blast_jobs` previously deleted **all** Jobs labelled
`app=blast|submit|setup|finalizer` in default ns before every submit.
Two users hitting the same cluster within seconds would have submit B
delete submit A's freshly-created Jobs.

Now the cleanup lists Jobs and only deletes those whose
`creationTimestamp` is **older than 30 minutes**. The timestamp is
parsed from K8s metadata; unparseable values are skipped (fail-safe).

### P0-4 / P0-5 — status check counted the wrong jobs

`k8s_check_blast_status` used to call `GET /apis/batch/v1/jobs` with no
label selector and aggregate `succeeded`/`failed`/`active` across **all**
Jobs in the namespace. That includes:

* `elb-submit-*` (our own helper Job) — completes in ~30 s, would fire
  the early "completed" path before BLAST started.
* `init-pv` / `elb-finalizer` (elastic-blast scaffolding).
* BLAST batch jobs from a *different* concurrent submit on the same
  cluster.

Now the status check:

* Filters Jobs by `labelSelector=app=blast` (matches the actual BLAST
  batch Jobs that elastic-blast creates).
* When `job_id` is supplied, additionally cross-references pods by the
  `BLAST_ELB_JOB_ID` env var to scope to the specific submit. Pod →
  `ownerReference.kind=Job` reduces back to the per-submit Job set.
* Returns `creating` (not `completed`) when no BLAST Jobs exist yet —
  this combined with the orchestrator's `MIN_POLLS_BEFORE_COMPLETE`
  guard removes the false-positive "completed at attempt 1" race.

The orchestrator now passes `job_id` into the activity payload so this
isolation actually applies.

## P1 — resource leaks & operator UX

### P1-1 — temp-file leak on session-build failure

`_get_k8s_session` wrote CA / client cert / key to `/tmp` then attached
cleanup only to `session.close`. If the AAD token fetch failed *after*
some files were written they leaked credential material on disk forever.
Now the body is wrapped in `try/except` that calls a shared
`_cleanup_temp_files()` helper before re-raising.

### P1-3 — submit-helper Job leak on timeout

When the submit poll loop ran out of attempts the orchestrator returned
`status=timeout` but the K8s Job lived on, accumulating into the
"stale" set the next submit would hit. Now the orchestrator calls a new
`cancel_elastic_blast_submit_activity` (with `propagationPolicy=Background`)
right before returning the timeout result. The activity is idempotent —
404 = already gone = success.

### P1-5 — stuck-pod diagnostic surface

`_get_submit_job_logs` already prints pod phase + waiting/terminated
reason for each container. Bumped `tailLines` to 400 so a 200-line
traceback before the EXIT_CODE marker no longer disappears.

### P1-6 — actionable error when `elb-openapi` missing

`RuntimeError("No elb-openapi pod found")` left users guessing. Now:

> No elb-openapi pod found in cluster. Deploy it from the AKS card
> (Provision Cluster runs this automatically; for an existing cluster
> use POST /api/aks/openapi/deploy).

## Validation

* `py_compile` clean for `api/activities/blast.py`,
  `api/services/monitoring.py`, `api/orchestrators/submit_blast.py`,
  `api/function_app.py`.
* `pytest -q api/tests/` → 16 passed (no regressions).
* Lint: only style errors (line length, hardcoded /tmp, unused imports
  pre-existing) — no new functional warnings.

## Out of scope

* Cluster-side per-submit namespace isolation (would require patches in
  `dotnetpower/elastic-blast-azure` itself; tracked separately).
* Submit Job's `time.sleep` in legacy `_submit_via_k8s_exec` — start/check
  split is the modern path; legacy path stays for VM Run Command fallback.
