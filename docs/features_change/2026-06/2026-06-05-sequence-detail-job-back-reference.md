---
title: Sequence Detail job back-reference card
description: Per-record card listing the caller's accession-mode BLAST jobs for a nuccore accession.
tags:
  - blast
  - user-guide
---

# Sequence Detail: "Your BLAST jobs for this accession" card

## Motivation

Closes [#25](https://github.com/dotnetpower/elb-dashboard/issues/25). The Sequence
Detail page mirrored NCBI nuccore data but offered no reason to stay in the
dashboard over clicking "Open in NCBI". This card surfaces the one thing NCBI can
never show — the researcher's own past BLAST runs against this accession — and
closes the loop between browsing a record and re-running a search on it.

## User-facing change

- A new glass card, **"Your BLAST jobs for this accession"**, renders on the
  Sequence Detail page directly under the record header, above the GenBank record.
- States: loading skeleton line, empty (worded to avoid implying "you never
  searched this sequence" — only accession-mode jobs are matched), populated
  (compact newest-first table of status / database / range / submitted, each row
  linking to the job detail), and degraded (calm warning line; the record view is
  never blocked).
- Owner-scoped: a caller only ever sees their own jobs.

## API / IaC diff summary

- **New route** `GET /api/blast/jobs/by-accession/{accession}?match=base|exact&limit=10`
  in [api/routes/blast/jobs.py](../../../api/routes/blast/jobs.py). Registered
  above `/jobs/{job_id}` so the literal `by-accession` segment is never captured
  as a `job_id`. `require_caller` enforced; scoped to `caller.object_id`. A
  jobstate failure degrades to `200 { degraded: true, reason: "jobstate_unavailable" }`
  (never 500).
- **New service** [api/services/blast/job_back_reference.py](../../../api/services/blast/job_back_reference.py)
  — `find_jobs_for_accession()` reads the caller's recent payloads
  (`include_payload=True`, bounded `SCAN_LIMIT=200`), matches
  `query_metadata.query_source == "ncbi_accession"` by version-stripped (`base`)
  or exact accession, and projects to a whitelisted, sanitised row shape. Phase 1,
  zero schema change.
- **Frontend**: typed client `blastApi.getJobsForAccession()` +
  `BlastJobForAccession` / `JobsForAccessionResponse` types in
  [web/src/api/blast.ts](../../../web/src/api/blast.ts); new
  [web/src/pages/sequence/JobBackReferenceCard.tsx](../../../web/src/pages/sequence/JobBackReferenceCard.tsx)
  (TanStack Query, `staleTime` 30 s); wired into
  [web/src/pages/sequence/SequenceDetail.tsx](../../../web/src/pages/sequence/SequenceDetail.tsx).
- No IaC change. No SAS tokens, no new browser-side external calls, no payload
  leakage beyond the whitelisted projection.

## Validation evidence

- `uv run pytest -q api/tests/test_job_back_reference.py` — 16 passed (helper
  matching, version normalisation, sub-range projection, non-accession /
  soft-deleted / non-blast exclusion, owner isolation, `limit` cap, db-URL
  subscription-id sanitisation, route auth, `limit`/`match` 422 caps, degraded
  fallback returns 200, no `/jobs/{job_id}` shadowing).
- `uv run pytest -q api/tests` — 2835 passed, 3 skipped.
- `uv run ruff check api` — clean.
- `cd web && npm run build` — green; `npx eslint` on the changed files — clean.

## Out of scope (tracked separately)

Defline-accession fallback for paste-mode submissions, top-hit enrichment, and the
Phase 2 denormalised column + backfill remain stretch follow-ups per the issue.
