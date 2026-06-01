---
title: Reproducible Methods citation for BLAST jobs
description: Per-job citation endpoint and Copy-citation button rendering a Methods paragraph, Markdown, or BibTeX from the job provenance bundle.
tags:
  - blast
  - user-guide
---

# Reproducible Methods citation for BLAST jobs

## Motivation

Researchers writing up a BLAST search need a Methods sentence that names the
exact program, BLAST+ version, and database snapshot they ran against, plus the
canonical literature references. Reconstructing this by hand is error-prone and
hurts reproducibility. The provenance for every job is already captured at
submit time, so the citation can be rendered deterministically with no extra
Azure calls.

## User-facing change

- New **Copy citation** button on the BLAST job header. It copies a ready-to-paste
  Methods paragraph (program, BLAST+ version, database, database snapshot,
  effective search space) to the clipboard.
- New backend route `GET /api/blast/jobs/{job_id}/citation?format={text|markdown|bibtex}`
  returning the rendered citation plus the structured fields it was built from.
  - `text` — Methods paragraph.
  - `markdown` — Methods paragraph with a references list.
  - `bibtex` — `@article` entries for Camacho 2009 (BLAST+) and Boratyn 2023
    (ElasticBLAST) plus a per-run `@misc` entry keyed by a short run id.
- The citation is owner-scoped: a non-owner caller receives 403, a missing job 404.
- No Storage URLs or SAS tokens are ever emitted (Charter §9).

## API / IaC diff summary

- New service `api/services/blast/citation.py` — `build_citation(...)` →
  `CitationBundle` (pure function over the provenance bundle, no Azure SDK).
- New route `blast_job_citation` in `api/routes/blast/jobs.py`.
- New typed client `blastApi.getCitation` + `BlastCitation` / `BlastCitationFormat`
  types in `web/src/api/blast.ts`.
- No infra change.

## Validation evidence

- `uv run pytest -q api/tests/test_blast_citation.py` — 7 passed (text/markdown/bibtex
  render, degrades without provenance, never emits storage URLs, HTTP owner check
  403 / missing 404).
- `cd web && npm run build` — clean.
- `cd web && npm test -- --run` — 454 passed.
