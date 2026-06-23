---
title: Split jobs are date-aware (the date-layout flag-flip gate) — #75
description: Split parent merge output, readiness probes, and path-key builders all resolve through resolve_results_prefix, so a dated split parent's Results read matches its merge write. Removes the last blocker for flipping STORAGE_DATE_LAYOUT_ENABLED on.
tags:
  - storage
  - blast
---

# Split jobs are date-aware

Epic #64, issue #75 (the #67 follow-up that gates flipping the flag).

## Problem

The results date layout (#67) shipped flag-gated, but split jobs were not
date-aware: the submit route stamps a dated `results_prefix` on every blast
submission (it cannot know at submit time the job will split), while
`tasks/blast/split_pipeline` built every split path as flat `{job_id}/...`. With
the flag on, a split parent's merge output (written flat) would never be found by
the Results page (which reads the dated prefix) → empty results for split jobs.

## Fix: one source of truth for every split path

Every split blob path flows through two builders —
`_parent_split_result_paths(parent)` and `_split_child_result_paths(child)` — and
the two `_result_blob_map` readiness probes. All four now resolve through
`resolve_results_prefix(job_id)`:

- **Parent** (stamped dated by submit when the flag is on) → merged result /
  merge report / manifest land under the dated prefix; the merge **write**
  (`_write_split_parent_result_artifacts`, which uses
  `_parent_split_result_paths`), the readiness **probe**, and the Results-page
  **read** all derive from the same resolved prefix → no desync.
- **Children** (created in `split_pipeline` without a dated stamp) resolve flat
  (`{child_job_id}/`) and stay self-consistent — the merge reads each child's
  output at its own flat prefix.

With the flag off, `resolve_results_prefix` returns `{job_id}/` with no Table
lookup, so split is byte-identical to the legacy behaviour.

## What changed

- `api/tasks/blast/split_pipeline.py` — `_parent_split_result_paths` /
  `_split_child_result_paths` and both `_result_blob_map` probes use
  `resolve_results_prefix` instead of the flat `{job_id}/`.
- `api/services/storage/job_prefix.py` — the `date_layout_enabled` docstring no
  longer lists split as a limitation; it now documents split as date-aware and
  keeps the remaining flip prerequisites (queries flat #74, soft-delete #76,
  live-cluster validation).

## Validation evidence

- `uv run pytest api/tests/test_split_date_aware.py` → **4 passed** (parent flat
  when flag off; parent dated when flag on + dated row; child flat even with flag
  on; degrade-to-flat on missing row).
- Flag-OFF regression: `pytest api/tests/test_blast_tasks.py -k "split or merge or
  parent or child"` → **58 passed** (byte-identical when off).
- `uv run ruff check api` → clean.

## Remaining flip prerequisites (still OFF by default)

Split is no longer a blocker, but flipping `STORAGE_DATE_LAYOUT_ENABLED` on still
needs: blob soft-delete (#76, recoverability net) and a **live-cluster end-to-end
validation** of a dated split job (the path consistency is unit-proven, but a real
split+merge run cannot be exercised without a cluster).

## Self-critique (design pass)

- **Contract/consistency**: write/probe/read all derive from the two builders →
  one source of truth; parent dated + child flat each self-consistent. ✓
- **Partial failure**: resolver degrades to flat on lookup failure (tested). ✓
- **Backward-compat**: flag off byte-identical (58 split tests). ✓
- **Perf**: each builder call does one Table lookup when the flag is on; split is
  not a hot path and the count is bounded by the number of split groups. ✓
- Verdict: no Critical/High; live-cluster validation noted as a flip prerequisite.
