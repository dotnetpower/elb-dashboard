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

## API change

`POST /api/blast/jobs/{job_id}/comparison` accepts
`{"against_job_id": "...", "max_items": 200}`. It independently checks
ownership and terminal-success state for both jobs, reads result blobs through
the existing bounded parallel reader, and returns a versioned read-only result.
It writes no job row, artifact, queue message, or completion event.

## Validation

- `uv run pytest -q api/tests/test_blast_result_comparison.py` - 3 passed.
- `uv run pytest -q api/tests/test_blast_result_comparison.py api/tests/test_blast_results_routes.py` - 44 passed before the compatibility follow-up.
- `npm --prefix web test -- --run src/pages/blastResults/ResultComparisonPanel.test.ts src/pages/blastResults/BlastResultsTabs.test.ts` - 5 passed.
- `npm --prefix web run build` - passed.
- Protected Service Bus source hashes remained identical to the pre-expansion
  baseline.