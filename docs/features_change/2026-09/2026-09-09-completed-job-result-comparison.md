---
title: Completed BLAST result comparison
description: Compare two owner-readable completed jobs without rerunning BLAST or mutating job state.
tags: [blast, ui]
---

# Completed BLAST result comparison

## Motivation

Researchers could inspect two result pages independently but could not answer
which query/subject hits were added, removed, or materially changed between
runs. Re-running a search was not required for this first comparison surface;
the existing completed outputs already contain the needed evidence.

## User-facing change

The Results page has a **Comparison** tab. The user selects or enters another
completed job id, then receives counts for common, unchanged, changed, added,
and removed hits plus a bounded change table. Both jobs must use the same known
program and database. Legacy rows with missing identity metadata remain
comparable rather than disappearing.

The response explicitly reports partial inputs and a truncated change list.
Comparison keys are `(query_id, subject_id)` and metrics aggregate every HSP for
that key before comparison.

Database identity is normalized before compatibility checks, so `core_nt` and
`blast-db/core_nt/core_nt` describe the same database. A change in HSP count is
reported even when both result formats omit e-values.

Directional labels describe the current page relative to the selected
comparison job: **added** exists only in the current result and **removed**
exists only in the selected baseline. The panel states that direction next to
the result.

A completed job with no parseable result artifact is unavailable for comparison
rather than treated as an empty hit set. At most the requested 500 change rows
are materialized; counts still describe the bounded parsed inputs, and long
identifiers are sanitized and length-bounded without collapsing distinct hits.

## API change

`POST /api/blast/jobs/{job_id}/comparison` accepts
`{"against_job_id": "...", "max_items": 200}`. It independently checks
ownership and terminal-success state for both jobs, reads result blobs through
the existing bounded parallel reader, and returns a versioned read-only result.
It writes no job row, artifact, queue message, or completion event.

## Validation

- `uv run pytest -q api/tests/test_blast_result_comparison.py` - 9 passed.
- `uv run pytest -q api/tests/test_blast_result_comparison.py api/tests/test_blast_results_routes.py` - 44 passed before the compatibility follow-up.
- `npm --prefix web test -- --run src/pages/blastResults/ResultComparisonPanel.test.ts src/pages/blastResults/BlastResultsTabs.test.ts` - 5 passed.
- `npm --prefix web run build` - passed.
- Desktop and mobile Playwright checks rendered summary values, the bounded
  change table, and the partial-input warning without page-level overflow.
- Protected Service Bus source hashes remained identical to the pre-expansion
  baseline.