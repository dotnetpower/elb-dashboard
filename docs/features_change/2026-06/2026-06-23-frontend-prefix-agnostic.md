---
title: Frontend result screens are prefix-agnostic; date-aware results-location hints
description: Verified the BLAST result data plane already fetches via server-provided file_id (no client-side {job_id}/ reconstruction), surfaced results_prefix in the job response, and made the two cosmetic results-location hints date-aware.
tags:
  - frontend
  - blast
---

# Frontend result screens: prefix-agnostic + date-aware hints

Epic #64, issue #72.

## Finding: the data plane was already prefix-agnostic

The substantive acceptance — "no component reconstructs storage paths from
`job_id`; all reads go through backend-provided IDs" — was **already satisfied**:

- `downloadResultFile(jobId, fileId, …)` hits `/blast/jobs/{job_id}/results/{file_id}`
  with the server-provided opaque `file_id` (base64 of the full blob name); the
  backend decodes it. `useBlastResultActions` downloads via `file.file_id`.
- Files / Alignments / Taxonomy / Graphic / export all consume the manifest's
  server-provided `name` / `file_id`. None reconstruct `{job_id}/`.

So Files/download/export/analytics already work for **both** flat and dated jobs,
and the "results purged" / empty case is handled by the existing
`degraded` / empty-manifest UI (no crash).

## What changed (the only flat-specific bits: cosmetic hints)

Two **display-only** strings showed a reconstructed flat `results/{jobId}/`
path, which would be cosmetically wrong for a dated job:

- `BlastStepTimeline/buildStepLog.ts` completion log ("Results container: …").
- `blastResults/StorageLockedPanel.tsx` manual-browse URL hint (shown only when
  Storage is network-locked).

Both are now date-aware:

- **Backend** (`api/services/blast/job_state.py` `_local_to_blast_job`) surfaces
  `results_prefix` in the job `infrastructure` block (additive; omitted when
  unset). Flat jobs surface `{job_id}/` (matches the fallback), dated jobs the
  date-tiered prefix.
- **Frontend** `blast.types.ts` adds `infrastructure.results_prefix?`;
  `buildStepLog` (already receives `job`) and `StorageLockedPanel` (threaded an
  optional `resultsPrefix` from `ResultsCard` → `ResultsBody`) prefer it, with a
  flat `{jobId}/` fallback.

## Validation evidence

- `cd web && npm run build` → green (typecheck of the `results_prefix` threading).
- `npx vitest run src/pages/blastResults src/components/BlastStepTimeline` →
  **161 + 44 passed**.
- `uv run pytest api/tests/test_local_to_blast_job.py` → **47 passed** (additive
  `results_prefix` in the projection is safe). `ruff check api` clean.

## Self-critique (design pass)

- **Contract**: `results_prefix` additive/optional everywhere; flat fallback when
  absent; data fetch path unchanged (still `file_id`). ✓
- **Security**: display-only hints; no SAS, no fetch-path change; the real read
  path stays `file_id`-validated (#70). ✓
- **Diff discipline**: an accidental interface-line deletion in `ResultsBody`
  was caught by re-reading + the build and restored (25 insertions / 2 intended
  deletions on final audit). ✓
- Verdict: no Critical/High.
