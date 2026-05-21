# BLAST Results Fast Polling

## Motivation

The BLAST results page could appear paused during warmup or staging because the browser issued heavy job detail, result listing, file preview, and database metadata requests while lightweight warmup status requests were completing quickly.

## User-Facing Change

The BLAST results page now keeps its polling path lighter before results are ready. Job detail polling skips database metadata enrichment, result listing waits for result-ready or terminal phases, and non-split job detail responses no longer query split child rows. Execution timing is also hardened so the timeline uses server-authored step timestamps, treats warmed SSD reuse as a skipped Stage DB step, and shows missing completed-step timings as `not measured` instead of silently omitting them.

## API/IaC Diff Summary

- Added `include_database_metadata=false` support to `GET /api/blast/jobs/{job_id}`.
- Limited Kubernetes refresh checks to runtime phases with a real ElasticBLAST runtime job id.
- Skipped split-child summary lookup for non-split job detail responses.
- Updated the BLAST results frontend polling path to request the lighter job detail shape and delay result manifest listing until the job phase is result-ready.
- Kept job detail polling active until the resolved phase is terminal, so a transient `status=completed` with a non-terminal phase cannot freeze the execution timeline on an earlier step.
- Started the warmup timing checkpoint before the readiness check, recorded warmed SSD reuse as an explicit skipped `staging_db` step, and stopped copying the full terminal execution duration into individual timeline steps.
- Preserved skipped-step semantics when late terminal completion details arrive, so warmed SSD reuse cannot be rewritten into a completed Stage DB row with submit logs attached.
- Narrowed skipped-step preservation to late successful completion updates only, so future failed/cancelled updates cannot be masked by an earlier skipped decision.
- Kept synchronous submit logs out of the final `completed` step; submit output remains on `submitting`, while `completed` only carries completion timing metadata.
- Added Kubernetes runtime timestamps to BLAST status responses and synthesize completed `running` / `exporting_results` timeline steps from those timestamps when a synchronous `elastic-blast submit` finishes.
- Added BLAST container and `results-export` container runtime spans to Kubernetes status responses so the UI can distinguish actual compute time from dashboard workflow time.
- Moved post-submit runtime reconciliation into the periodic worker path: accepted BLAST jobs now refresh K8s state asynchronously, close the BLAST runtime step when Kubernetes completes, remain in `results_pending` until parseable result artifacts exist, and only then transition to `completed` and enqueue artifact finalization.
- Added an async completed-job runtime metric backfill task. It scans completed BLAST rows with missing container metrics, reuses stored or discovered ElasticBLAST `job-*` IDs, updates only the stored runtime K8s payload, and preserves the original job `updated_at` so historical workflow elapsed time is not inflated.
- Ignored unparseable Kubernetes timestamps when deriving runtime bounds, so one malformed pod/job timestamp cannot collapse the whole BLAST status refresh to `unknown`.
- Hardened the timeline UI to prefer server timestamp durations, render skipped steps with their decision reason, and show `not measured` when legacy completed steps have no authoritative duration.
- Added runtime chips to the BLAST result header for workflow elapsed time, BLAST container compute span, K8s runtime, submit path, and result-export container span. If container export span is not available yet, the UI labels the fallback as `Export path` instead of implying container-level timing.
- No IaC changes.

## Validation Evidence

- `uv run pytest -q api/tests/test_blast_jobs_routes.py api/tests/test_local_to_blast_job.py` -> `14 passed in 1.83s`.
- `uv run ruff check api/routes/blast/jobs.py api/tests/test_blast_jobs_routes.py api/tests/test_local_to_blast_job.py` -> `All checks passed!`.
- `npm run build` in `web/` -> successful Vite production build with only the existing chunk-size warning.
- Local API health smoke: `GET /api/health` -> `200` in `0.001802s`.
- Patched job detail smoke: `GET /api/blast/jobs/5e46da6c-4bad-4615-ad27-ade40d85d779?include_database_metadata=false` -> `200`, no `database_metadata`, no `split_children`, repeated `1.49-1.64s` locally.
- Live BLAST rerun: job `18b9edca-4d8a-41bc-9cd8-3a2f161259b6`, task `4f2311b4-4e35-49a9-80d5-a9930e892d8c` -> `SUCCESS`, dashboard state `completed/completed`.
- Generated `elastic-blast.ini` included `exp-use-local-ssd = true` and `exp-skip-warmed-ssd-init = true`; warmup evidence reported 10 ready nodes.
- Runtime evidence: ElasticBLAST job `job-429825360482416da20736cf3ed51a95`, 10 Kubernetes jobs/pods, 10 succeeded, 0 failed; results manifest `available` with 72 files and 34 parseable files.
- Browser verification: `/blast/jobs/18b9edca-4d8a-41bc-9cd8-3a2f161259b6?tab=run` shows Job Completed Successfully and all execution steps checked, including `Stage DB 32s` and `Complete`.
- Follow-up live rerun: dashboard job `9b45dbfe-1c63-433e-a650-609e2d43bbd8` completed in the browser; `Stage DB 33s`, `Submit Job 2m 18s`, `BLAST Run`, `Export`, and `Complete` all rendered with check marks. Runtime evidence reported Kubernetes job `job-849df6d46604495ab978e4c2a2c55045`, 10 jobs/pods, 10 succeeded, 0 failed, and the Descriptions tab loaded 100 hits from `merged_results.out.gz`.
- Timeline hardening tests: `uv run pytest -q api/tests/test_k8s_blast_status.py api/tests/test_blast_tasks.py` -> `104 passed in 14.37s`.
- Final live validation rerun: dashboard job `579dba4e-9546-481b-b1c1-9aed80e4037d` completed. Warmup was measured as `12s`, warmed SSD reuse rendered as skipped Stage DB, submit was measured from `2026-05-21T03:36:23+00:00` to `2026-05-21T03:38:10Z`, BLAST runtime was synthesized from Kubernetes timestamps as `20s`, export as `64s`, and the results API returned 72 files.
- Skipped-step regression: `uv run pytest -q api/tests/test_blast_tasks.py -k 'warmed_ssd or late_terminal_details or splits_submit_runtime'` -> `3 passed, 98 deselected in 1.55s`.
- Targeted backend regression after final hardening: `uv run pytest -q api/tests/test_blast_tasks.py api/tests/test_k8s_blast_status.py api/tests/test_blast_jobs_routes.py` -> `106 passed in 22.66s`.
- Critique hardening regression: `uv run pytest -q api/tests/test_blast_tasks.py -k 'warmed_ssd or late_terminal_details or overrides_existing_skipped or keeps_submit_log_out or splits_submit_runtime'` -> `5 passed, 98 deselected in 1.17s`.
- K8s timestamp hardening regression: `uv run pytest -q api/tests/test_k8s_blast_status.py` -> `5 passed in 1.01s`.
- Targeted Python lint after critique hardening: `uv run ruff check api/services/k8s_monitoring.py api/tasks/blast/progress.py api/tests/test_blast_tasks.py api/tests/test_k8s_blast_status.py` -> `All checks passed!`.
- Full API lint after critique hardening: `cd /home/moonchoi/dev/elb-dashboard && uv run ruff check api` -> `All checks passed!`.
- Full backend regression after final hardening: `PYTHONPATH=$PWD uv run pytest -q api/tests` -> `825 passed in 40.90s`.
- Full backend regression after critique hardening: `PYTHONPATH=$PWD uv run pytest -q api/tests` -> `828 passed in 38.93s`.
- Backend lint after final hardening: `uv run ruff check api` -> `All checks passed!`.
- Focused backend lint: `uv run ruff check api/tasks/blast/progress.py api/tasks/blast/__init__.py api/services/k8s_monitoring.py api/tests/test_blast_tasks.py api/tests/test_k8s_blast_status.py` -> `All checks passed!`.
- Frontend validation after timeline changes: `npm run build` in `web/` -> successful Vite production build with only the existing chunk-size warning.
- Frontend validation after critique hardening: `npm run build` in `web/` -> successful Vite production build with only the existing chunk-size warning.
- Post-hardening live BLAST rerun: dashboard job `bb61858a-8cb6-4590-a2e3-c144662851f7` completed from the browser. Final server state was `status=completed`, `phase=completed`, with `Warmup Check 15s`, `Stage DB skipped`, `Submit Job 1m 46s`, `BLAST Run 20s` from `k8s_runtime`, `Export 1m 9s`, and `Complete 0s`; the final completed step had no submit log payload. The Descriptions tab loaded `100 shown, 100 filtered of 100 hits`, and the Files tab reported manifest `available · 34/72` with primary outputs including `merged_results.out.gz`.
- Runtime separation follow-up: raw Kubernetes evidence for dashboard job `bb61858a-8cb6-4590-a2e3-c144662851f7` showed dashboard workflow `4m14s`, UI step sum `3m45s`, K8s BLAST job span `20s`, BLAST container compute span `7s`, result-export container span `14s`, and finalizer job span `41s`.
- Runtime metric backend regression: `uv run pytest -q api/tests/test_k8s_blast_status.py` -> `6 passed in 0.99s`.
- Runtime metric frontend regression: `npm run test -- BlastJobHeader.test.ts` in `web/` -> `2 passed in 830ms`.
- Runtime metric regression: `uv run pytest -q api/tests/test_local_to_blast_job.py api/tests/test_k8s_blast_status.py` -> `19 passed in 1.04s`.
- Runtime chip frontend regression after fallback-label hardening: `npm run test -- BlastJobHeader.test.ts` in `web/` -> `3 passed in 868ms`.
- Full API lint after runtime metric follow-up: `cd /home/moonchoi/dev/elb-dashboard && uv run ruff check api` -> `All checks passed!`.
- Full backend regression after runtime metric follow-up: `cd /home/moonchoi/dev/elb-dashboard && uv run pytest -q api/tests` -> `832 passed in 43.92s`.
- Frontend build after runtime metric follow-up: `cd /home/moonchoi/dev/elb-dashboard/web && npm run build` -> successful Vite production build in `9.56s` with only the existing chunk-size warning.
- Browser check after runtime chips: `http://127.0.0.1:8090/blast/jobs/bb61858a-8cb6-4590-a2e3-c144662851f7?tab=run` rendered `Workflow 4m 14s`, `K8s runtime 20s`, `Submit path 1m 46s`, and export timing from the currently stored payload. Existing completed payloads created before the container-span code do not show `Compute`; new submissions do once the updated worker records K8s container metrics.
- Worker-side runtime reconciliation regression: `PYTHONPATH=$PWD uv run pytest -q api/tests/test_blast_tasks.py -k 'results_pending or reconcile_k8s or reconcile_submit or external_refresh'` -> `7 passed, 99 deselected in 1.12s`.
- Targeted backend regression after worker-side reconciliation: `PYTHONPATH=$PWD uv run pytest -q api/tests/test_blast_tasks.py api/tests/test_k8s_blast_status.py api/tests/test_local_to_blast_job.py` -> `125 passed in 16.38s`.
- Full API lint after worker-side reconciliation: `uv run ruff check api` -> `All checks passed!`.
- Full backend regression after worker-side reconciliation: `PYTHONPATH=$PWD uv run pytest -q api/tests` -> `835 passed in 39.28s`.
- Diff whitespace check after worker-side reconciliation: `git diff --check` -> no output.
- Local API smoke after worker-side reconciliation: `GET /api/health` on `127.0.0.1:8085` -> `200` in `0.003432s`; `GET /api/blast/jobs?limit=1` -> `200` in `2.551711s`.
- Completed-job runtime backfill focused tests: `PYTHONPATH=$PWD uv run pytest -q api/tests/test_blast_tasks.py -k 'backfill_completed_runtime_metrics'` -> `4 passed, 106 deselected in 1.40s`.
- Completed-job repository filter test: `PYTHONPATH=$PWD uv run pytest -q api/tests/test_state_repo.py -k 'list_completed'` -> `1 passed, 10 deselected in 1.29s`.
- Targeted backend regression after completed-job backfill: `PYTHONPATH=$PWD uv run pytest -q api/tests/test_blast_tasks.py api/tests/test_k8s_blast_status.py api/tests/test_local_to_blast_job.py api/tests/test_state_repo.py` -> `140 passed in 18.88s`.
- Full API lint after completed-job backfill: `uv run ruff check api` -> `All checks passed!`.
- Full backend regression after completed-job backfill: `PYTHONPATH=$PWD uv run pytest -q api/tests` -> `840 passed in 39.59s`.
- Completed-job backfill browser evidence: `backfill_completed_runtime_metrics.run(job_id='bb61858a-8cb6-4590-a2e3-c144662851f7')` -> `{'scanned': 1, 'backfilled': 1, 'skipped': 0, 'errors': 0}`; refreshed Run details rendered `Workflow 4m 12s`, `Compute 7s`, `K8s runtime 20s`, `Submit path 1m 46s`, and `Export containers 14s`.