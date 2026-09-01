---
title: Preserve near-miss variants at sharded BLAST cutoffs
description: Keep one lower-scoring candidate when a tied top-score class fills the merged max_target_seqs window, with matching XML and tabular behavior.
tags:
  - blast
  - user-guide
---

# Preserve near-miss variants at sharded BLAST cutoffs

## Motivation

A partitioned [BLAST+](https://blast.ncbi.nlm.nih.gov/doc/blast-help/) search can
produce more perfect-score ties than `max_target_seqs`. The global shard merge
previously kept only that tied score class, so a slightly lower-scoring sequence
carrying a biologically relevant variation could disappear even when it was
present in the shard outputs. The existing diversity-aware selector was disabled
by default and applied only to tabular output, while the external API defaults to
XML output.

## User-facing change

- XML (`outfmt 5`) and tabular (`outfmt 6` or `7`) merges now use the same
  diversity-aware cutoff.
- When all selected hits belong to one `(evalue, bitscore)` class and a lower
  score exists, the final slot is used for the best lower-scoring near-miss by
  default. This intentionally changes the previous strict score-only default;
  the output remains capped at `max_target_seqs`.
- `ELB_DIVERSITY_AWARE_CUTOFF=0` restores strict score-only top-N behavior. A
  positive value reserves up to that many slots.
- A strict tie-order oracle takes precedence over diversity reservation so a
  captured NCBI accession set is never modified.
- The result notice prioritizes the near-miss preservation message when both tie
  overflow and reservation are present.
- The merge cannot restore a hit already removed by a shard-local
  `max_target_seqs` cutoff. Raising the requested limit remains necessary when
  broader candidate coverage is required.

## API / IaC diff summary

- `terminal/merge-sharded-results.sh` defaults the reservation to one slot and
  applies the existing selector and report fields to XML merges.
- `merge-report.json` records `diversity_reserved_count` and
  `diversity_queries` for both supported merge families.
- The result API shape is unchanged; the existing optional `tie_cutoff` payload
  carries the XML signal without a contract migration.
- No IaC or Azure resource change is required.

## Validation evidence

- `uv run pytest -q api/tests/test_sharded_merge.py -m '' -k 'diversity_aware
  or strict_tie_order_oracle_disables'` - 7 passed, covering default tabular/XML
  preservation, explicit strict mode, exact-`N` and `N=1` boundaries, duplicate
  subject HSP filtering, strict-oracle precedence, and a two-slot reservation.
- `uv run pytest -q api/tests` - 5,545 passed, 4 skipped. The skips require a
  live candidate-result directory or the unavailable sibling source checkout.
- `ELB_TERMINAL_IMAGE=ncbi/blast:2.17.0
  scripts/dev/verify-local-blast-xml-sharding.sh` - real BLAST+ full-DB and
  two-shard XML hit order and HSP tuples matched exactly.
- `npm --prefix web test -- --run` - 990 passed.
- `uv run ruff check api`, `npm --prefix web run lint`,
  `npm --prefix web run build`, and `bash -n terminal/merge-sharded-results.sh`
  - passed.
- `uv run python scripts/docs/check_frontmatter.py` and
  `DISABLE_MKDOCS_2_WARNING=true uv run mkdocs build --strict` - passed.
- Host-mode Playwright check at desktop and `390x844` - the preservation notice
  and near-miss row rendered; mobile document width remained within the viewport.