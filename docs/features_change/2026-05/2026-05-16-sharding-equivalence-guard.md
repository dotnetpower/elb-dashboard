# 2026-05-16 — Sharding equivalence guard

## Motivation

The warmed-DB sharding path was designed for local-SSD performance, but a
full-DB BLAST+ or NCBI Web BLAST result is the correctness reference. The
current shard execution path does not yet guarantee equivalent output:

- BLAST e-value statistics can use shard-local database size unless a full
  DB size is injected into every shard run.
- `max_target_seqs` is applied inside each shard before any global merge.
- The upstream finalizer merge is tabular-output oriented and does not cover
  XML, pairwise output, custom fields, tie-breaking, or full BLAST headers.
- The Azure local-SSD template pins shard index to node ordinal, so selecting
  more shards than nodes can produce unschedulable jobs.

Until there is an end-to-end equivalence gate, sharding must not be the default
submit path.

## User-facing change

Dashboard BLAST submits now preserve full-DB semantics by default even when a
database has pre-created shard layouts. The generated ElasticBLAST config no
longer injects `db-partitions`, `db-partition-prefix`, or
`exp-use-local-ssd=true` automatically just because DB metadata says
`sharded=true`.

Benchmark or power-user callers can still opt into the current experimental
path by passing `allow_approximate_sharding=true` in submit options. That opt-in
is intentionally named to avoid implying result equivalence.

When full BLAST DB statistics are available from the DB's `.njs` metadata, the
experimental shard path now injects `-dbsize <full-db-letters>` so shard-local
statistics are closer to full-DB BLAST. For single-query validation runs, callers
can pass `db_effective_search_space=<full-run-searchsp>` to inject `-searchsp`,
which reproduces full-DB e-values for that query shape. This is still not a
general equivalence guarantee for arbitrary multi-query jobs, so the explicit
opt-in remains required.

Partitioned/sharded result merge is also part of the precision path. The
terminal image now patches the vendored `elastic-blast-azure` finalizer so
partitioned Azure jobs always submit the finalizer, the finalizer receives the
BLAST options, and the merged shard output uses the requested
`-max_target_seqs` instead of a hard-coded 500. Sharded submits are restricted
to merge-compatible tabular output (`outfmt 6` / `outfmt 6 std...`) until the
finalizer supports XML/JSON/custom-column merge semantics.

The precision path is now staged explicitly:

1. `sharding_mode` contract (`off | approximate | precise`) and pre-flight
  precision report.
2. FASTA query metadata parsing for query count/length policy.
3. Submit-time precise-mode gate before Celery queueing.
4. Standalone sharded-result merge engine with `merge-report.json`.
5. Multi-query precise mode is allowed only when each query has an explicitly
  supplied effective search space, provided as an ordered list matching FASTA
  query order, and every value is identical. Mixed search spaces remain blocked
  until query-group job splitting is implemented. This uniform-value gate is a
  conservative execution limitation; it does not infer scientific suitability
  from query length alone.
6. Frontend sharding mode selector and local comparison harness for repeated
  full-vs-sharded checks.

The dashboard's DB auto-partition checkbox is now off by default and labelled as
experimental. When a user explicitly turns it on, the SPA sends the same
`allow_approximate_sharding=true` opt-in.

If the opt-in path selects more shards than available nodes, config generation
now fails fast with a clear error instead of producing a Kubernetes job set that
cannot schedule all shard ordinals.

## API / IaC diff summary

- `api/services/blast_config.py`
  - Default auto-shard injection is disabled.
  - New explicit opt-in option: `allow_approximate_sharding`.
  - Sharded configs use full-DB `-dbsize` when `db_total_letters` metadata is
    available.
  - `db_effective_search_space` can inject `-searchsp` for precise single-query
    validation runs.
  - Sharded submits reject non-tabular output formats that the finalizer cannot
    merge correctly.
- `api/services/sharding_precision.py`, `api/services/query_metadata.py`
  - Add a reusable precision policy report and FASTA metadata parser.
  - Precise sharding currently requires query metadata plus either
    `db_effective_search_space` or uniform `query_effective_search_spaces`.
  - `query_effective_search_spaces` must be an ordered list; mapping/dict input
    is rejected so query-order semantics cannot be lost.
  - Query metadata now preserves full FASTA headers and sequence lines for safe
    future split execution, while API responses omit raw sequence payloads.
  - Duplicate FASTA query IDs are rejected because deterministic tabular merge
    groups hits by query id.
- `api/services/query_grouping.py`
  - Add the pure query-group planning primitive for the next mixed-search-space
    precise path. It groups FASTA queries by effective search space while
    preserving first-seen order.
  - Add group FASTA materialization so each planned query group can be rendered
    into a separate FASTA payload without losing headers or sequence lines. It
    still does not dispatch split BLAST jobs.
  - Add a pure split-execution planner that prepares per-group child job ids,
    query blob paths, FASTA payloads, and group-specific `-searchsp` options.
    This is the handoff contract for a future Celery dispatcher; it still does
    not upload blobs or enqueue group jobs.
- `api/services/storage_data.py`, `api/tasks/blast.py`
  - Add the task-side upload primitive for split query FASTA files. It uploads
    group FASTA payloads under the `queries/split/<job>/<group>/query.fa` prefix
    and returns state-safe metadata only; raw FASTA is not included in returned
    payloads. Group job submission and aggregation are still future work.
  - Add the state-safe child submit planner for uploaded split query groups. It
    builds per-child ElasticBLAST config content and idempotent submit argv from
    uploaded blob metadata, while rejecting option keys that could override the
    parent resource/cluster/storage context. It still does not execute terminal
    submits or aggregate child results.
  - Add the child submit dispatcher helper. It creates `blast-child` state rows,
    records `parent_job_id`, submits each child config through the terminal
    sidecar, and persists only state-safe metadata. Parent task integration,
    cascading cancellation, and result aggregation remain future work.
  - Add the parent split execution helper that, given in-memory FASTA text,
    parses query metadata, builds the mixed-search-space split plan, uploads
    group FASTA files, builds child configs, dispatches child submits, and
    updates the parent phase. Raw FASTA is not included in return values or
    state/history payloads. Reading the original query FASTA from Storage and
    automatically branching the public submit task remain future work.
  - Harden split upload and source-query handling. Split FASTA uploads are
    verified by reading a small prefix back from the `queries` container. A new
    internal Storage-backed parent helper reads the original `query_file` from
    the `queries` container with an explicit 100 MiB cap, rejects split-result
    paths and non-FASTA payloads, drops the raw FASTA reference after dispatch,
    and still does not expose mixed precise splitting through public submit.
  - Add split child state aggregation for the future result-merge step. The
    helper summarizes `blast-child` rows by `parent_job_id`, detects running,
    failed, cancelled, missing, invalid, and possibly truncated child sets, and
    moves the parent only to an intermediate `split_children_merge_ready` phase
    when all children are complete. It intentionally does not mark the parent
    `completed`; the future merge/finalizer integration owns that transition.
  - Add split parent result finalization. The parent verifies that each completed
    child has `merged_results.out.gz` and `merge-report.json`, waits in
    `split_results_waiting_for_artifacts` if a finalizer artifact is missing,
    concatenates child gzip result members into `results/<parent>/merged_results.out.gz`,
    writes parent `merge-report.json` and `split-results-manifest.json`, and only
    then marks the parent `completed`. Parent assembly does not re-rank hits
    because split query groups are disjoint.
  - Open the public mixed precise submit path. Ordered, per-query mixed
    `query_effective_search_spaces` now pre-flight as
    `precise_tabular_split`, and `/api/blast/submit` routes those requests to
    the Storage-backed split parent helper instead of generating a single
    ElasticBLAST config. Direct config generation still rejects split-required
    mixed query requests so they cannot accidentally bypass the parent/child
    flow.
  - Add split parent status and cancellation integration. Status checks for a
    parent job aggregate child rows and run the finalizer when all child
    artifacts are ready. Cancelling a parent cascades to all child Kubernetes
    jobs and marks child state rows cancelled.
- `api/services/state_repo.py`
  - Add `parent_job_id` to `JobState` plus a child-list query so split children
    can be discovered from their parent job.
- `api/routes/stubs.py`
  - `/api/blast/pre-flight` returns a sharding precision report and inline FASTA
    query metadata.
  - `/api/blast/submit` blocks invalid precise sharding before queueing Celery.
  - Local job list/detail responses include a state-safe `split_children`
    summary for split parent jobs.
  - `db_auto_partition=true` now also requires that opt-in because upstream
    auto-partitioning has the same result-equivalence caveat.
  - Added a scheduler guard: selected shard count must be less than or equal to
    `num_nodes` for the current Azure local-SSD shard template.
- `api/services/db_sharding.py`, `api/services/storage_data.py`
  - Parse BLAST v5 `.njs` metadata (`number-of-letters`,
    `number-of-sequences`, cache bytes) and surface it as dashboard/job metadata.
- `api/tests/test_blast_config_sharding.py`
  - Updated expectations so default config generation does not shard.
  - Added opt-in coverage, precise-stat option coverage, and a `shards > nodes`
    refusal test.
- `api/tests/test_blast_tasks.py`
  - Updated task-level metadata resolution tests so metadata discovery alone
    does not trigger sharding.
  - Added explicit opt-in coverage.
- `web/src/pages/blastSubmitModel.ts`, `web/src/pages/BlastSubmit.tsx`,
  `web/src/pages/blastSubmit/ComputeSection.tsx`, `web/src/api/blast.ts`,
  `web/src/pages/BlastJobs.tsx`, `web/src/components/BlastStepTimeline.tsx`,
  `web/src/constants.ts`
  - DB auto-partition defaults to off.
  - The checkbox copy marks the path experimental.
  - The SPA sends `allow_approximate_sharding=true` only when the user turns
    on DB auto-partition.
  - Saved submit drafts are versioned; older drafts that had
    `db_auto_partition=true` are migrated back to the new safe default.
  - Job rows show split child counts/status summaries, and split parent phases
    map to the existing timeline and dashboard colors.
- `terminal/Dockerfile`, `terminal/patch_elastic_blast.py`
  - Patch the terminal-side `elastic-blast-azure` clone at image build time so
    sharded Azure jobs always submit the finalizer/merger.
  - Pass `ELB_BLAST_OPTIONS` into the finalizer and derive merge top-N from
    `-max_target_seqs`.
  - Inject `merge-sharded-results.sh` into the upstream scripts ConfigMap path
    and upload `merge-report.json` alongside `merged_results.out.gz`.
- `terminal/merge-sharded-results.sh`
  - Merge tabular shard outputs by query, e-value, bit score, and deterministic
    tie order; emit unsupported-row and tie warnings.
- `scripts/dev/compare-sharded-results.py`
  - Local harness for comparing full BLAST outfmt 6/std output against merged
    shard outputs without launching AKS.

No IaC change.

## Validation evidence

Focused backend tests:

```bash
uv run pytest -q api/tests/test_blast_config_sharding.py api/tests/test_blast_tasks.py api/tests/test_db_sharding.py
```

Result:

```text
80 passed in 6.59s
```

Focused precision-path tests:

```bash
uv run pytest -q api/tests/test_query_metadata.py api/tests/test_query_grouping.py api/tests/test_storage_data.py api/tests/test_state_repo.py api/tests/test_sharding_precision.py api/tests/test_sharded_merge.py api/tests/test_blast_config_sharding.py api/tests/test_blast_tasks.py::test_upload_split_query_files_returns_state_safe_metadata api/tests/test_blast_tasks.py::test_build_split_child_submit_plan_generates_group_configs api/tests/test_blast_tasks.py::test_build_split_child_submit_plan_rejects_unsafe_option_override api/tests/test_blast_tasks.py::test_build_split_child_submit_plan_rejects_incomplete_group api/tests/test_blast_tasks.py::test_dispatch_split_child_submits_creates_state_and_runs_terminal api/tests/test_blast_tasks.py::test_dispatch_split_child_submits_records_terminal_failure api/tests/test_blast_tasks.py::test_run_split_parent_submission_dispatches_children_without_raw_fasta api/tests/test_blast_tasks.py::test_run_split_parent_submission_requires_mixed_search_spaces api/tests/test_blast_tasks.py::test_run_split_parent_submission_marks_parent_failed_when_child_fails api/tests/test_smoke.py::test_blast_submit_blocks_invalid_precise_sharding_before_queue api/tests/test_smoke.py::test_blast_preflight_blocks_precise_multi_query api/tests/test_smoke.py::test_blast_preflight_allows_precise_multi_query_uniform_search_space api/tests/test_smoke.py::test_blast_submit_blocks_precise_mapping_search_spaces
```

Result:

```text
76 passed in 1.06s
```

Touched-file lint after parent split helper:

```bash
uv run ruff check api/tasks/blast.py api/tests/test_blast_tasks.py
```

Result:

```text
All checks passed!
```

Storage-backed split helper hardening tests:

```bash
uv run pytest -q api/tests/test_blast_tasks.py::test_upload_split_query_files_returns_state_safe_metadata api/tests/test_blast_tasks.py::test_upload_split_query_files_verifies_uploaded_blob api/tests/test_blast_tasks.py::test_query_blob_path_from_query_file_accepts_queries_paths api/tests/test_blast_tasks.py::test_query_blob_path_from_query_file_rejects_unsafe_inputs api/tests/test_blast_tasks.py::test_run_storage_query_split_parent_submission_reads_blob_and_drops_raw_fasta api/tests/test_blast_tasks.py::test_run_storage_query_split_parent_submission_rejects_non_fasta api/tests/test_blast_tasks.py::test_run_storage_query_split_parent_submission_rejects_oversized_query api/tests/test_blast_tasks.py::test_run_storage_query_split_parent_submission_reports_missing_query_file
```

Result:

```text
14 passed in 0.80s
```

Expanded precision-path focused regression after Storage-backed helper:

```bash
uv run pytest -q api/tests/test_query_metadata.py api/tests/test_query_grouping.py api/tests/test_storage_data.py api/tests/test_state_repo.py api/tests/test_sharding_precision.py api/tests/test_sharded_merge.py api/tests/test_blast_config_sharding.py api/tests/test_blast_tasks.py::test_upload_split_query_files_returns_state_safe_metadata api/tests/test_blast_tasks.py::test_upload_split_query_files_verifies_uploaded_blob api/tests/test_blast_tasks.py::test_build_split_child_submit_plan_generates_group_configs api/tests/test_blast_tasks.py::test_build_split_child_submit_plan_rejects_unsafe_option_override api/tests/test_blast_tasks.py::test_build_split_child_submit_plan_rejects_incomplete_group api/tests/test_blast_tasks.py::test_dispatch_split_child_submits_creates_state_and_runs_terminal api/tests/test_blast_tasks.py::test_dispatch_split_child_submits_records_terminal_failure api/tests/test_blast_tasks.py::test_run_split_parent_submission_dispatches_children_without_raw_fasta api/tests/test_blast_tasks.py::test_run_split_parent_submission_requires_mixed_search_spaces api/tests/test_blast_tasks.py::test_run_split_parent_submission_marks_parent_failed_when_child_fails api/tests/test_blast_tasks.py::test_query_blob_path_from_query_file_accepts_queries_paths api/tests/test_blast_tasks.py::test_query_blob_path_from_query_file_rejects_unsafe_inputs api/tests/test_blast_tasks.py::test_run_storage_query_split_parent_submission_reads_blob_and_drops_raw_fasta api/tests/test_blast_tasks.py::test_run_storage_query_split_parent_submission_rejects_non_fasta api/tests/test_blast_tasks.py::test_run_storage_query_split_parent_submission_rejects_oversized_query api/tests/test_blast_tasks.py::test_run_storage_query_split_parent_submission_reports_missing_query_file api/tests/test_smoke.py::test_blast_submit_blocks_invalid_precise_sharding_before_queue api/tests/test_smoke.py::test_blast_preflight_blocks_precise_multi_query api/tests/test_smoke.py::test_blast_preflight_allows_precise_multi_query_uniform_search_space api/tests/test_smoke.py::test_blast_submit_blocks_precise_mapping_search_spaces
```

Result:

```text
95 passed in 5.24s
```

Split child aggregation hardening tests:

```bash
uv run pytest -q api/tests/test_blast_tasks.py::test_aggregate_split_child_states_marks_merge_ready_without_completing_parent api/tests/test_blast_tasks.py::test_aggregate_split_child_states_reports_running_and_missing_children api/tests/test_blast_tasks.py::test_aggregate_split_child_states_marks_parent_failed_on_failed_child api/tests/test_blast_tasks.py::test_aggregate_split_child_states_marks_parent_cancelled_on_cancelled_child api/tests/test_blast_tasks.py::test_aggregate_split_child_states_rejects_empty_children api/tests/test_blast_tasks.py::test_aggregate_split_child_states_rejects_unknown_status api/tests/test_blast_tasks.py::test_aggregate_split_child_states_rejects_more_children_than_expected api/tests/test_blast_tasks.py::test_aggregate_split_child_states_rejects_possible_truncation
```

Result:

```text
8 passed in 0.64s
```

Expanded precision-path focused regression after aggregation helper:

```bash
uv run pytest -q api/tests/test_query_metadata.py api/tests/test_query_grouping.py api/tests/test_storage_data.py api/tests/test_state_repo.py api/tests/test_sharding_precision.py api/tests/test_sharded_merge.py api/tests/test_blast_config_sharding.py api/tests/test_blast_tasks.py::test_upload_split_query_files_returns_state_safe_metadata api/tests/test_blast_tasks.py::test_upload_split_query_files_verifies_uploaded_blob api/tests/test_blast_tasks.py::test_build_split_child_submit_plan_generates_group_configs api/tests/test_blast_tasks.py::test_build_split_child_submit_plan_rejects_unsafe_option_override api/tests/test_blast_tasks.py::test_build_split_child_submit_plan_rejects_incomplete_group api/tests/test_blast_tasks.py::test_dispatch_split_child_submits_creates_state_and_runs_terminal api/tests/test_blast_tasks.py::test_dispatch_split_child_submits_records_terminal_failure api/tests/test_blast_tasks.py::test_run_split_parent_submission_dispatches_children_without_raw_fasta api/tests/test_blast_tasks.py::test_run_split_parent_submission_requires_mixed_search_spaces api/tests/test_blast_tasks.py::test_run_split_parent_submission_marks_parent_failed_when_child_fails api/tests/test_blast_tasks.py::test_query_blob_path_from_query_file_accepts_queries_paths api/tests/test_blast_tasks.py::test_query_blob_path_from_query_file_rejects_unsafe_inputs api/tests/test_blast_tasks.py::test_run_storage_query_split_parent_submission_reads_blob_and_drops_raw_fasta api/tests/test_blast_tasks.py::test_run_storage_query_split_parent_submission_rejects_non_fasta api/tests/test_blast_tasks.py::test_run_storage_query_split_parent_submission_rejects_oversized_query api/tests/test_blast_tasks.py::test_run_storage_query_split_parent_submission_reports_missing_query_file api/tests/test_blast_tasks.py::test_aggregate_split_child_states_marks_merge_ready_without_completing_parent api/tests/test_blast_tasks.py::test_aggregate_split_child_states_reports_running_and_missing_children api/tests/test_blast_tasks.py::test_aggregate_split_child_states_marks_parent_failed_on_failed_child api/tests/test_blast_tasks.py::test_aggregate_split_child_states_marks_parent_cancelled_on_cancelled_child api/tests/test_blast_tasks.py::test_aggregate_split_child_states_rejects_empty_children api/tests/test_blast_tasks.py::test_aggregate_split_child_states_rejects_unknown_status api/tests/test_blast_tasks.py::test_aggregate_split_child_states_rejects_more_children_than_expected api/tests/test_blast_tasks.py::test_aggregate_split_child_states_rejects_possible_truncation api/tests/test_smoke.py::test_blast_submit_blocks_invalid_precise_sharding_before_queue api/tests/test_smoke.py::test_blast_preflight_blocks_precise_multi_query api/tests/test_smoke.py::test_blast_preflight_allows_precise_multi_query_uniform_search_space api/tests/test_smoke.py::test_blast_submit_blocks_precise_mapping_search_spaces
```

Result:

```text
103 passed in 5.51s
```

Frontend build after sharding mode UI:

```bash
cd web && npm run build
```

Result:

```text
✓ built in 4.79s
```

Local comparison harness smoke:

```bash
python3 scripts/dev/compare-sharded-results.py --full full.tsv --shard shard.tsv --max-target-seqs 10
```

Result excerpt:

```json
{
  "exact_line_sets_equal": true,
  "exact_ordered_rows_equal": true,
  "full_rows": 1,
  "merged_rows": 1
}
```

Terminal upstream patch dry-run:

```bash
tmpdir=$(mktemp -d) \
  && cp -a /home/moonchoi/dev/elastic-blast-azure/. "$tmpdir/" \
  && python3 terminal/patch_elastic_blast.py "$tmpdir"
```

Result:

```text
patched elastic-blast-azure finalizer for sharded result merge
```

Full backend suite:

```bash
uv run pytest -q api/tests
```

Result:

```text
373 passed in 37.11s
```

Split finalizer focused tests:

```bash
uv run pytest -q api/tests/test_blast_tasks.py::test_verify_split_child_result_artifacts_detects_missing_report api/tests/test_blast_tasks.py::test_verify_split_child_result_artifacts_requires_completed_child api/tests/test_blast_tasks.py::test_write_split_parent_result_artifacts_concats_child_gzip_and_report api/tests/test_blast_tasks.py::test_finalize_split_parent_results_waits_for_missing_child_artifacts api/tests/test_blast_tasks.py::test_finalize_split_parent_results_completes_after_artifacts_written api/tests/test_blast_tasks.py::test_finalize_split_parent_results_is_idempotent_when_parent_artifacts_exist
```

Result:

```text
6 passed in 1.09s
```

Split submit/status/cancel smoke:

```bash
uv run pytest -q api/tests/test_blast_tasks.py api/tests/test_smoke.py
```

Result:

```text
90 passed in 6.72s
```

Local split parent submit-to-finalize e2e:

```bash
uv run pytest -q api/tests/test_blast_tasks.py::test_split_parent_storage_submit_to_finalize_e2e
```

Result:

```text
1 passed in 0.97s
```

Backend lint subset:

```bash
uv run ruff check api/services/blast_config.py api/services/sharding_precision.py api/tasks/blast.py api/tests/test_blast_tasks.py api/tests/test_smoke.py api/tests/test_sharding_precision.py api/tests/test_storage_data.py api/services/storage_data.py
```

Result:

```text
All checks passed!
```

Frontend build:

```bash
cd web && npm run build
```

Result:

```text
✓ built in 4.95s
```

Local BLAST+ precision probe:

- Built a synthetic BLAST DB (`elb_compare_tiny`) with 8 volumes and two shard
  aliases, using `ncbi/blast:2.17.0`.
- Full DB baseline: `blastn -db elb_compare_tiny` returned one hit with
  e-value `2.67e-47`.
- Naive sharded concat returned the same hit coordinates but e-value `1.43e-47`.
- Adding only `-dbsize 29999612` removed the threshold mismatch but still
  produced e-value `2.71e-47`.
- Adding full-run `-searchsp 2254169736` to each shard made the sorted tabular
  line sets exactly equal:

```text
full_rows=1
sharded_rows=1
exact_line_sets_equal=True
matched_line=query_motif seq_00257 ... 2.67e-47 185
```

Browser smoke:

- Reloaded `/blast/submit` after the Vite HMR connection failed.
- Confirmed the checkbox label is
  `DB auto-partition (experimental; may differ from full-DB BLAST)`.
- Confirmed the DB auto-partition checkbox is unchecked after saved draft
  migration.

Static checks:

- `git --no-pager diff --check` on the touched files produced no output.
- VS Code diagnostics reported no errors in the touched backend and frontend
  source/test files.