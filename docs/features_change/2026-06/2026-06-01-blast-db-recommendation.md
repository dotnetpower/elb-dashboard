---
title: BLAST database recommendation endpoint
description: A versioned static oracle that recommends a BLAST database from molecule, program, goal, and taxon.
tags:
  - blast
  - user-guide
---

# BLAST database recommendation endpoint

## Motivation

New users often do not know which database to point a search at — `core_nt` vs
`nt`, `nr` vs `refseq_protein` vs `swissprot`. NCBI Web BLAST nudges the user
toward a sensible default based on what they are trying to do. A small,
versioned rule table can give the same nudge with no Azure calls and a stable,
testable contract.

## User-facing change

- New backend route `GET /api/blast/databases/recommend` with query params
  `molecule` (`dna|protein`), `program`, `goal`
  (`identify|highly_similar|transcripts|genomes|well_characterized|comprehensive`,
  default `identify`), and optional `taxon`.
- Returns a recommended database plus an alternative, each with a short rationale,
  and the ruleset version (`2026-06-01`). When a `taxon` is supplied the response
  notes that taxonomy is applied as a `-taxids` filter, not a database switch.

This is the backend contract for an upcoming submit-form database hint; the
endpoint is live and tested now.

## API / IaC diff summary

- New service `api/services/blast/db_recommendation.py` — `recommend_database(...)`
  → `Recommendation` over a static `_RULES` table (pure function, no Azure SDK).
- New route `blast_databases_recommend` in `api/routes/blast/databases.py`.
  Distinct from the existing `/databases/{db_name}/oracle` route, which is a
  shard-order pointer optimization, not a database selection helper.
- No infra change.

## Validation evidence

- `uv run pytest -q api/tests/test_blast_db_recommendation.py` — 8 passed
  (per-goal rules, program→molecule inference, taxon note, HTTP route).
- `uv run ruff check api` — all checks passed.
