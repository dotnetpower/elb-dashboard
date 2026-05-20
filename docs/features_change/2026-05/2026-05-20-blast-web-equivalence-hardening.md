# BLAST Web Equivalence Hardening

## Motivation

Actual MPXV F3L example runs exposed two user-visible gaps in the BLAST result flow:

- A submit task could briefly mark a dashboard job `completed` before parseable result files existed.
- The frontend defaulted to `-soft_masking true`, while the verified NCBI Web BLAST-compatible MPXV/core_nt configuration requires `-dust yes -soft_masking false` unless the user explicitly enables lookup-table-only masking.

## User-facing Change

- Running jobs that have reached Kubernetes completion but have not produced parseable result files now remain `running/results_pending` instead of showing as complete.
- The result page explains that final BLAST result files are still being prepared instead of claiming the completed job has missing files.
- New BLAST searches default to Web-compatible hard masking for nucleotide low-complexity filtering. Users can still enable `Mask for lookup table only` to send `-soft_masking true` explicitly.
- Precise sharded submissions now opt into prepared DB-order oracle metadata when available, and strict tie-order oracle submissions widen the shard-local candidate pool so the finalizer can actually find the Web-selected accessions.
- Concurrent `elastic-blast submit` calls are serialized in the worker to avoid Kubernetes `init-ssd-*` immutable Job patch conflicts.
- The finalizer now looks for tie-order oracle metadata under both the ElasticBLAST internal result prefix and the parent dashboard job prefix, so oracle files uploaded before the internal `job-*` id is known are still applied during merge. Strict oracle merge reports also include missing oracle accession diagnostics for DB snapshot gaps.

## API / UI Diff Summary

- `api.tasks.blast.submit` gates `completed` on parseable result artifacts.
- `api.services.blast_job_state._refresh_running_blast_state` also refuses to promote running jobs to completed until result artifacts exist.
- `api.tasks.blast.reconcile_stale_jobs` no longer treats Celery `SUCCESS` from a submit task as BLAST completion when the task result still says `running` or when completed artifacts are absent.
- `web/src/pages/blastSubmitModel.ts` and `web/src/pages/blastSubmit/useSubmitMutation.ts` default to `-soft_masking false` for BLASTN low-complexity filtering.
- `web/src/pages/blastSubmit/useSubmitMutation.ts` includes `use_db_order_oracle` for precise sharded submissions.
- `api.tasks.blast.submit` serializes terminal-side `elastic-blast submit` invocations with a Redis lease and expands strict tie-order oracle runs to `-max_target_seqs 5000` unless the user already requested a wider pool.
- `api.services.monitoring.k8s_check_blast_status` now filters Kubernetes Jobs by `elb-job-id` even before scoped pods exist, avoiding false completion from another active BLAST run.
- `terminal/patch_elastic_blast.py` patches the ElasticBLAST finalizer script to search both `${ELB_RESULTS}/metadata` and the parent dashboard-prefix `metadata` directory for `tie-order-oracle*.txt` files before invoking `merge-sharded-results.sh`.
- `terminal/merge-sharded-results.sh` records `tie_order_oracle_missing_count` and `tie_order_oracle_missing_queries` when strict oracle accessions are absent from the shard result pool.
- BLAST result tabs render pending-result copy while the job is still running.

## Validation Evidence

- Focused backend: `uv run ruff check api/tasks/blast/__init__.py api/services/blast_job_state.py api/tests/test_blast_tasks.py api/tests/test_local_to_blast_job.py`
- Focused backend: `PYTHONPATH=$PWD uv run pytest -q api/tests/test_blast_tasks.py::test_gate_completed_submit_waits_for_result_artifacts api/tests/test_blast_tasks.py::test_gate_completed_submit_allows_completed_with_result_artifacts api/tests/test_blast_tasks.py::test_reconcile_celery_success_marks_row_completed api/tests/test_blast_tasks.py::test_reconcile_submit_success_keeps_running_row_running api/tests/test_blast_tasks.py::test_reconcile_submit_completed_waits_for_result_artifacts api/tests/test_local_to_blast_job.py::test_refresh_running_blast_state_waits_for_result_artifacts`
- Focused frontend: `npm run test -- --run src/pages/blastResults/analytics/blastAnalyticsState.test.ts src/pages/blastSubmit/taxonomyFilter.test.ts`
- Browser: job `6db7b6f4-480a-40f6-9765-1201dac9e8ad` renders `RESULTS_PENDING` and explains that final BLAST result files are still being prepared.
- Real BLAST: job `9d633524-4633-42af-83c8-63ff789f7afc` produced `merged_results.out.gz`; aggregate parses `1 / 1` files with `100` hits.
- NCBI comparison evidence: Web RID `0TACF1Z1016` showed that `-soft_masking true` produces mismatched raw/bit scores (`462` / `854.272`) versus Web BLAST (`448` / `828.419`), motivating the default hard-masking fix.
- NCBI comparison evidence: corrected job `6db7b6f4-480a-40f6-9765-1201dac9e8ad` with `-dust yes -soft_masking false` matched Web BLAST primary HSP values (`score=448`, `bits=828.419`, `value_mismatch_count=0`).
- NCBI comparison evidence: DB-order oracle job `9400ebe6-4487-4457-a461-7077445a6f30` still had `top100_overlap=4` and `value_mismatch_count=0`, confirming exact Web row membership/order requires a Web accession oracle for this tied MPXV/core_nt window.
- Strict Web oracle run: job `eb5771a0-b20f-437b-a21f-ec62670c1bdf`, internal ElasticBLAST id `job-091df19b32144d09940cbb659b928ce9`, task `1cb94c03-dd3f-4ab8-ad91-3e3b956c0f86` completed and produced `merged_results.out.gz`.
- Strict Web oracle finalizer evidence: patched `elb-scripts` rerun produced `merge-report.json` with `ranking_basis=best_hsp_evalue_bitscore_oracle_ordinal`, `tie_order_oracle_accessions=100`, `tie_order_oracle_strict=true`, `tie_order_oracle_missing_count=1`, `first_missing_accessions=[OZ470124.1]`, and `total_output_hits=99`.
- NCBI comparison evidence: `docs/temp/blast-equivalence-20260520/compare-strict-oracle-v2-report.json` had `shared_accessions=99`, `top100_overlap=99`, and `value_mismatch_count=0`; the only Web-only accession was `OZ470124.1`.
- DB snapshot evidence: raw shard XML search found no `OZ470124.1`, and `blastdbcmd -db /blast/blastdb/core_nt_shard_00..09 -entry OZ470124.1` returned `Entry not found` on all 10 node-local shards. The remaining 1/100 Web difference is therefore a local `core_nt` snapshot gap, not a result parser, finalizer ordering, or UI issue.
- Browser evidence: `http://127.0.0.1:8090/blast/jobs/eb5771a0-b20f-437b-a21f-ec62670c1bdf` rendered `99 shown, 99 filtered of 99 hits` in Descriptions, and the Alignments tab rendered actual Query/Sbjct alignment blocks after data load.
- Focused backend after concurrency/status hardening: `PYTHONPATH=$PWD uv run pytest -q api/tests/test_k8s_blast_status.py api/tests/test_local_to_blast_job.py::test_refresh_running_blast_state_waits_for_result_artifacts api/tests/test_blast_submit_route_options.py api/tests/test_blast_oracles.py`
- Full backend: `PYTHONPATH=$PWD uv run pytest -q api/tests` (`722 passed`).
- Full frontend: `npm run test -- --run` (`193 passed`) and `npm run build` (passed with the existing chunk-size warning).