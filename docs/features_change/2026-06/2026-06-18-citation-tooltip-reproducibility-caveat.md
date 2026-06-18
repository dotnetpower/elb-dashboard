---
title: Methods citation tooltip notes audit-row reproducibility caveat
description: The Run-details "Copy citation" tooltip now states the Methods paragraph is reproducible only while the job's audit row is retained, completing roadmap item #2 R4.
tags:
  - blast
  - research
  - ui
---

# Methods citation reproducibility caveat (#2 R4)

## Motivation

Roadmap item #2 **R4 — one-click "Methods paragraph" for publications** is
already shipped end-to-end: `api/services/blast/citation.py` builds a reproducible
Methods paragraph from the persisted provenance bundle alone, the
`GET /api/blast/jobs/{id}/citation?format=text|markdown|bibtex` route exposes it,
and the Run-details header carries a **Copy citation** button
(`web/src/pages/blastResults/BlastJobHeader.tsx`).

The one missing R4 acceptance item was the tooltip caveat: the citation is only
reproducible while the job's audit/provenance row exists, because the paragraph
is synthesised from that row (no live Azure calls at render time). Deleting the
job removes the provenance the citation is built from.

## User-facing change

The **Copy citation** button tooltip now reads:

> Copy a reproducible Methods citation (program, version, database snapshot) to
> the clipboard. Reproducible only while this job's audit row is retained —
> deleting the job removes the provenance the citation is built from.

No behavioural / API change; tooltip wording only.

## API / IaC diff summary

- `web/src/pages/blastResults/BlastJobHeader.tsx`: extended the Copy-citation
  button `title` with the audit-row reproducibility caveat.

## Validation evidence

- `cd web && npm test -- --run src/pages/blastResults/BlastJobHeader.test.ts` —
  10 passed.
- `cd web && npm run build` — ok; `npx eslint BlastJobHeader.tsx` — clean.
- Backend `uv run pytest -q api/tests/test_blast_citation.py` — 8 passed
  (citation service / route unchanged).
