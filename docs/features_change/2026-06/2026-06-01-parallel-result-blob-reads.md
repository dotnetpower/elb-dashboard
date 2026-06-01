---
title: Parallel result-blob reads for BLAST export and aggregate
description: Fan out the serial result-blob reads behind BLAST export/aggregate onto a bounded thread pool to cut wall-clock latency without changing output.
tags:
  - blast
  - architecture
---

# Parallel result-blob reads for BLAST export and aggregate

## Motivation

The risk audit flagged three serial blob-read loops as a genuine bottleneck. Each
iterated `result_blobs[:RESULTS_MAX_FILES]` (up to 20 files) and issued one
Storage round-trip at a time before parsing:

* `blast_job_results_export` (CSV/JSON export hits aggregation),
* `_export_raw_result_text` (raw XML/text export),
* `build_result_aggregate_payload` (aggregate analytics on cache-miss).

A 20-shard job therefore paid 20 sequential network reads even though the reads
are independent. The blob-service clients are thread-local and pooled, so the
reads are safe to run concurrently — they were simply serialised.

## User-facing change

* BLAST result **export** (CSV/JSON, and raw XML/text) and the **aggregate**
  analytics fallback complete faster on multi-shard jobs. Output bytes, ordering,
  error codes, and degraded states are unchanged.
* No feature added or removed. This is a latency-only change.

## API / IaC diff summary

* `api/services/blast/result_analytics.py`
  * New helper `read_result_blob_texts_parallel(...)`: reads a list of result
    blobs concurrently on a bounded `ThreadPoolExecutor`
    (`_RESULT_READ_MAX_WORKERS = 8`), **preserving input order** via
    `executor.map`. Returns `(blob_path, content, error)` per blob so callers
    keep their existing per-blob failure accounting. Single-blob and empty lists
    short-circuit without spawning threads.
* `api/services/blast/result_artifacts.py`
  * `build_result_aggregate_payload` now reads via the helper. Read **and** parse
    failures are still counted into `read_failures` exactly as before (the parse
    stays inside the per-blob `try`).
* `api/routes/blast/results.py`
  * `blast_job_results_export` and `_export_raw_result_text` now read via the
    helper. The `503 all_reads_failed` guard, the `409 format_not_captured` /
    `409 multiple_xml_reports` guards, the XML/text content filtering, and the
    ordered concatenation of the raw export are all preserved (order kept by
    `executor.map`). Removed two now-unused local
    `from api.services.storage.data import read_result_blob_text` imports.
* `_read_hits` in `result_artifacts.py` is intentionally left serial: its
  `max_hits` early-break is order-dependent, so parallelising it could change
  which hits land in a truncated result set. No behaviour change is acceptable
  there.

## Validation evidence

* `uv run ruff check api/services/blast/result_analytics.py api/services/blast/result_artifacts.py api/routes/blast/results.py` — all checks passed.
* `uv run pytest -q api/tests/test_blast_results_routes.py api/tests/test_blast_result_manifest.py api/tests/test_blast_result_analytics_organism.py api/tests/test_blast_results_parser.py api/tests/test_blast_tasks.py` — 196 passed.
* `uv run pytest -q api/tests/test_job_artifacts.py api/tests/test_blast_ncbi_report.py api/tests/test_storage_data.py` — 51 passed (these monkeypatch `read_result_blob_text` on the shared `storage_data` module, which the new helper still resolves at call time).
* `uv run pytest -q api/tests` — 2378 passed, 3 skipped (unchanged baseline).
