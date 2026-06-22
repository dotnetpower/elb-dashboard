---
title: ElasticBLAST program enum-sync CI guard (#56)
description: A CI-enforceable guard that fails when ElasticBLAST advertises a BLAST program our submit-time program×database handling has not added.
tags:
  - blast
  - contributor
---

# ElasticBLAST program enum-sync guard (#56)

## Motivation

Issue #56 (roadmap R1′) acceptance item 3 asks for a CI check that fails if
ElasticBLAST adds a BLAST program the dashboard has not added. The earlier
program×database compatibility guard
([2026-06-21](2026-06-21-program-database-compatibility-guard.md)) deferred this
item as a "cross-repo wiring decision" because the sibling
`dotnetpower/elastic-blast-azure` program list is not a dependency of this
repo's CI image. The maintainer cleared that decision (both repos are
owned), so the guard now ships.

## User-facing change

None — this is a contributor/CI guard only. No runtime behaviour, route, or
schema changed.

## Change summary

- New `api/tests/test_blast_program_enum_sync.py`:
  - `ELASTIC_BLAST_ADVERTISED_PROGRAMS` — a vendored snapshot of
    `ElbSupportedPrograms._programs` from the sibling repo
    (`blastn, blastp, blastx, psiblast, rpsblast, rpstblastn, tblastn,
    tblastx`). This is the CI-enforceable pin: hosted CI (no sibling checkout)
    runs `test_compat_map_covers_advertised_programs`, which fails if
    `_PROGRAM_TO_DB_MOLECULE` stops covering an advertised program.
  - `test_snapshot_matches_sibling_when_available` — parses the sibling
    `src/elastic_blast/util.py` via AST (no import of the sibling package) and
    fails the moment upstream adds/removes a program, prompting a snapshot
    refresh here. Skips cleanly when the checkout is absent; honors
    `ELB_ELASTIC_BLAST_SRC` (repo root or the `util.py` path) and defaults to
    `~/dev/elastic-blast-azure`.

The two-test chain gives real enforcement without a network dependency in hosted
CI: the sibling-compare test forces a snapshot refresh on upstream drift, and
the snapshot then forces the compatibility map to cover any new program.

## #56 status after this change

- Item 2 (program×database compatibility guard) — shipped (commit `f4863fa`).
- Item 3 (CI enum-sync guard) — shipped here.
- Item 1 (expose `psiblast` / `rpsblast` in the `/blast/submit` dropdown) —
  still open: the submit dropdown intentionally lists the five end-to-end
  validated programs. Exposing `psiblast` (iterative / PSSM controls) and
  `rpsblast` (needs a conserved-domain `cdd` database in the warmup catalog)
  requires live validation before they are surfaced, so they stay out of the
  picker. #56 remains open for that item.

## Validation

* `uv run pytest -q api/tests/test_blast_program_enum_sync.py` — 2 passed
  (sibling-compare ran against the local `~/dev/elastic-blast-azure` checkout and
  matched the snapshot; compat-map coverage green).
* `uv run ruff check api/tests/test_blast_program_enum_sync.py` — clean.
