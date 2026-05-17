# 2026-05-17 — BLAST XML comparator

## Motivation

Web BLAST equivalence needs a reproducible comparator that checks biological and
statistical result fields instead of relying on byte-for-byte XML identity.

## User-facing Change

- Added `scripts/dev/compare-blast-xml.py`, a manual evidence helper for
  comparing Web BLAST XML, local full DB XML, and sharded merged XML.
- The comparator ignores provenance-only `BlastOutput_db` path differences by
  default, but reports normalized DB names and supports `--strict-db` when the
  DB name itself must match.
- The comparator compares query fields, per-query statistics, hit ordering,
  subject fields, HSP coordinates, aligned sequences, identity/gaps, e-values,
  and bit scores.

## API / IaC Diff Summary

- No API route changes.
- No frontend changes.
- No IaC changes.

## Validation Evidence

- `uv run pytest -q api/tests/test_compare_blast_xml.py` — 3 passed.
- `uv run ruff check scripts/dev/compare-blast-xml.py api/tests/test_compare_blast_xml.py` — passed.
- `python scripts/dev/compare-blast-xml.py --left docs/temp/core-nt-searchsp/extracted/results/core_nt.full.default.xml --right docs/temp/core-nt-searchsp/fresh-2026-05-17/live-finalizer-5be97da5/merged_results.out.gz --json docs/temp/core-nt-searchsp/fresh-2026-05-17/live-finalizer-5be97da5/canonical-compare.json` — `equivalent: true`, `difference_count: 0`.