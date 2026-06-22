---
title: Submit-time program × database molecule compatibility guard (#56)
description: Reject an unambiguous program/database molecule mismatch (e.g. blastp against a nucleotide DB) at submit with a clear 4xx instead of a ~30-minute pod failure.
tags:
  - blast
  - user-guide
---

# Submit-time program × database compatibility guard (#56)

## Motivation

Issue #56 (roadmap R1′) was re-scoped to a residual gap: the `/blast/submit`
path had no validation that the chosen BLAST **program** is compatible with the
chosen **database** molecule type. Submitting e.g. `blastp` (a protein search)
against a nucleotide database like `core_nt` was accepted, scheduled on the
cluster, and only failed ~30 minutes later as a pod error — an expensive, opaque
failure for the researcher.

## User-facing change

A submit whose program and database have an **unambiguous, known** molecule
mismatch now fails immediately with HTTP 422 and a clear message, e.g.:

> blastp searches a protein database, but 'core_nt' is a nucleotide database.
> Choose a protein database, or a program that searches nucleotide space.

Valid submits are unaffected. **Custom or unrecognised databases are never
rejected** — the guard is deliberately conservative.

## Design (best-effort, conservative)

- `api/services/blast/db_recommendation.py`:
  - `_PROGRAM_TO_DB_MOLECULE` gained `rpsblast` / `rpstblastn` (protein).
  - New `_KNOWN_DATABASE_MOLECULE` — a curated allow-list mapping well-known NCBI
    databases (core_nt, nt, refseq_rna, … → `dna`; nr, refseq_protein, swissprot,
    cdd, … → `protein`) to their molecule type.
  - New `program_database_compatibility_error(program, database)` returns a
    human message ONLY when BOTH the program AND the database are known and their
    molecule types disagree; it returns `None` (allow) whenever either side is
    unknown. So a user's custom BLAST DB or a future BLAST+ program is never
    falsely rejected — only a guaranteed mismatch is blocked.
- `api/routes/blast/submit.py` `_validate_submit_contracts` calls the guard
  before any side effects and raises `422 {code: "program_database_incompatible"}`.

## Not in this change (deferred to maintainer / cross-repo)

The second #56 acceptance item — a **CI enum-sync guard** that fails when
ElasticBLAST advertises a program our OpenAPI enum is missing — was deferred
here as a cross-repo wiring decision. It has since shipped; see
[2026-06-22-program-enum-sync-guard.md](2026-06-22-program-enum-sync-guard.md).
#56 stays open only for acceptance item 1 (exposing `psiblast` / `rpsblast` in
the submit dropdown, which needs live validation).

## Validation

* `uv run pytest -q api/tests/test_blast_db_recommendation.py` — 10 passed
  (2 new tests: blocks known mismatch incl. URL-form DB; allows valid pairings
  and every unknown side).
* `uv run pytest -q api/tests/ -k submit` — 250 passed (existing submits intact).
* `uv run pytest -q api/tests` — 4141 passed, 3 skipped.
* `uv run ruff check api` — clean.
