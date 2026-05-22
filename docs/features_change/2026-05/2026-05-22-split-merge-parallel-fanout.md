# Parallelize split-child report + artifact downloads

## Motivation
Two split-merge code paths were strictly sequential:

* `_load_split_child_merge_reports` — 1 HTTPS RTT per child to fetch the
  tiny `merge-report.json`. A 100-shard split paid 100 sequential round
  trips before the parent merge could even start aggregating.
* `_verify_split_child_result_artifacts` — 1 `_result_blob_map` call per
  child (which itself does a `list_blobs(prefix=child/)`). Same N×RTT
  shape blocking the finalize task.

Total: a 100-shard parent merge wasted ~200 sequential HTTPS round trips
before any productive work happened.

## User-facing change
None semantically — same dicts returned in the same input order. Latency
on the parent merge step drops to roughly the slowest single child report
read + slowest single list call (×4 concurrency bucket).

## API / IaC diff
* `api/tasks/blast/split_pipeline.py`
  * `_load_split_child_merge_reports` now fans out via
    `ThreadPoolExecutor(max_workers=min(4, len(children)))` and uses
    `pool.map` so the returned list keeps input order. Concurrency cap
    matches the existing `stream_blob_bytes` 4-permit budget so we do
    not exceed the BlobServiceClient pool.
  * `_verify_split_child_result_artifacts` keeps the upfront
    "not-completed → ValueError" validation sequential, then probes
    every child's blob map in parallel through the same fan-out shape.
    Missing-artifact aggregation stays sequential since it only walks
    the already-materialized status dicts.

## Validation
* `uv run pytest -q api/tests/test_blast_tasks.py` — 120 passed (XML +
  tabular merge + verify_split_child_result_artifacts cases unchanged).
* `uv run ruff check api/tasks/blast/split_pipeline.py` — clean.
