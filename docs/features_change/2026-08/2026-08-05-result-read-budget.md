---
title: Cap the per-job result-read memory budget
description: The parallel result reader returns a list, so up to 20 decoded blobs were alive at once — 200-400 MB in a 2 GiB sidecar. Read in batches against a per-job byte budget and report skipped files instead of silently under-reporting.
tags: [blast, operate]
---

# Cap the per-job result-read memory budget

## Motivation

This closes a gap that was written down and left open in the
[2026-06-21 large-XML change note](../2026-06/2026-06-21-blast-results-large-xml-parse.md):

> "Bounded by api-sidecar memory under the 20-file (`RESULTS_MAX_FILES`) worst
> case, so it needs a **per-job total-bytes budget** rather than a blanket cap
> bump."

`read_result_blob_texts_parallel` returned `list(executor.map(...))`. The
per-file caps bound **one** file, but because the return value is a list every
decoded string is alive simultaneously:

| Caller | Per-file cap | Files | Held at once |
| --- | --- | --- | --- |
| `build_result_aggregate_payload` | `RESULTS_AGGREGATE_MAX_BYTES` 10 MB | 20 | **200 MB** |
| `results_export` | `RESULTS_EXPORT_MAX_BYTES` 10 MB | 20 | **200 MB** |
| alignments artifact | `RESULTS_ALIGNMENTS_MAX_BYTES` 20 MB | 20 | **400 MB** |

In a 2 GiB sidecar that is a large fraction of the budget for a single request,
and it is exactly the class of peak the
[K8s list paging change](2026-08-05-k8s-list-paging.md) fixed on the monitoring
side. Unlike the monitoring routes this one is not polled, so it did not cause
the OOM incident — but it is the same defect and was already known.

## User-facing change

None for normal jobs: the budget (64 MiB) is far above what a realistic
multi-shard result set reads. A job that would have exceeded it now reports the
un-read files as read failures and marks the result partial, instead of the api
sidecar quietly allocating hundreds of megabytes.

## Change summary

* [api/services/blast/result_analytics.py](../../../api/services/blast/result_analytics.py):
  * `RESULTS_READ_TOTAL_MAX_BYTES = 64 MiB` — the per-job ceiling on decoded
    result text held at once, overridable with the env var of the same name.
  * `_result_read_total_budget()` — resolves the budget and **floors it at one
    full file**. A budget that could skip file #1 would turn a normal export
    into an `all_reads_failed` 503, so that is structurally prevented rather
    than left to configuration.
  * `read_result_blob_texts_parallel` now submits in **batches of `max_workers`**
    and stops once the budget is spent. Batching is what makes the budget real —
    a single `executor.map` over all 20 paths has already materialised every
    string before any accounting could run.
  * New `ResultReadBudgetExceeded`, carried in the tuple's `error` slot for
    skipped blobs. Both existing caller loops
    (`result_artifacts.build_result_aggregate_payload`,
    `routes/blast/results_export`) already do `if read_exc is not None: raise
    read_exc` inside their `try`, so skipped files are counted and logged with
    **no caller changes** — and the "every read failed" guards keep working.
    Silently returning `None` content would have under-reported hits with no
    signal at all.
  * A WARNING names the budget, the bytes read and the skipped count.

Ordering, the empty-blob-name placeholder tuple, and the single-blob fast path
are all unchanged.

## Validation

* `uv run pytest -q api/tests/test_result_read_budget.py` — 7 passed: the budget
  stops further reads and flags the skipped blobs; input order is preserved; the
  first blob is always read even with an absurd 1-byte budget; the budget floors
  at one full file; the env override works and a typo falls back to the default
  instead of disabling the ceiling; an under-budget job behaves exactly as
  before; empty blob names still yield the placeholder tuple.
* `uv run pytest -q api/tests -k result` — 247 passed (existing result suites).
* `uv run pytest -q api/tests` — **4986 passed**, 3 skipped.
* `uv run ruff check api` — clean.

## Still open

`routes/blast/result_analytics.py` passes a fully materialised `str` into
`parse_blast_result_content`. The parser already uses `iterparse` internally, so
only the input needs to become a stream — the worker-side
[split_pipeline.py](../../../api/tasks/blast/split_pipeline.py) shows the
pattern. With the per-file cap plus this per-job budget the peak is now bounded,
so that change is a fidelity improvement (parse past the byte cap), not a memory
fix.
