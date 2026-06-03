---
title: BLAST score-class truncation badge, near-miss preservation, and bit-score explanation
description: Surface tied-score truncation in the results UI, add opt-in deterministic and diversity-aware merge cutoffs, and explain absolute bit-score differences vs NCBI Web BLAST.
tags:
  - blast
  - user-guide
---

# BLAST score-class truncation badge, near-miss preservation, and bit-score explanation

## Motivation

Comparing an elb-dashboard run against NCBI Web BLAST for a SARS-CoV-2 ORF1ab
query (`NC_045512.2:266-21555`, `core_nt`) surfaced five gaps. ElasticBLAST
splits the database into shards and applies `-max_target_seqs` on each shard
before the merge. When a large database returns many subjects tied on the exact
same top score, the merged top-N is only a *sample* of that tied class — and a
near-perfect lower-scoring hit (e.g. `OU121186`) can fall below the cutoff with
no signal to the researcher. The absolute bit score also differs slightly from
NCBI (39286.7 vs 39316) purely because the effective search space differs with
database size, which can be mistaken for a scoring error.

## User-facing change

- **A — Score-class truncation badge.** The Descriptions tab now shows a notice
  when the displayed top hits are a sample of a larger tied score class
  (`overflow_count > 0`), including the `max_target_seqs` in effect and a tooltip
  explaining the per-shard cutoff and the remedy (re-run with a higher
  `-max_target_seqs`). When the opt-in diversity-aware cutoff reserved slots, the
  badge instead explains the displayed set is intentionally not the strict
  top-N-by-score.
- **B — Diversity-aware cutoff (opt-in, default OFF).** With
  `ELB_DIVERSITY_AWARE_CUTOFF=<k>` the tabular merge reserves up to `k` slots in
  a fully-tied top window for the best lower-scoring near-miss hits so a
  near-perfect match is not dropped. Default behaviour (strict top-N by score) is
  byte-for-byte unchanged.
- **C — max_target_seqs uplift.** Already user-adjustable via `-max_target_seqs`
  (merge default 500); no default change. The "raise max_target_seqs" guidance is
  surfaced by the A badge.
- **D — Deterministic tie ordering (opt-in, default OFF).** With
  `ELB_DETERMINISTIC_TIE_ORDER=1` the tabular and XML merges break score ties by
  subject accession instead of shard arrival ordinal, making cross-rerun
  selection reproducible. Default ordinal ordering is unchanged.
- **E — Bit-score explanation.** The Max / Total bit-score column tooltip now
  explains that the absolute bit score depends on the database's effective
  search space, so the same alignment can show a slightly different bit score
  than NCBI Web BLAST without being an error; relative ranking is unaffected.

## API / IaC diff summary

- `terminal/merge-sharded-results.sh`: new env-gated helpers
  `deterministic_tie_order_enabled` (`ELB_DETERMINISTIC_TIE_ORDER`),
  `diversity_aware_cutoff_limit` (`ELB_DIVERSITY_AWARE_CUTOFF`),
  `tie_break_sort_component`, `ranking_basis_label`, `apply_diversity_reservation`.
  `merge-report.json` gains `diversity_reserved_count`, `diversity_queries`, and a
  `ranking_basis` that reflects the active tie-break mode. Cutoff-overflow
  detection runs on the pristine top-`max_hits` window before any diversity
  reservation.
- `api/tasks/blast/split_pipeline.py`: `_aggregate_split_merge_reports` propagates
  `diversity_reserved_count` and `diversity_queries` (capped at 10) alongside the
  existing `tie_cutoff_*` aggregation.
- `api/services/blast/result_artifacts.py`: new best-effort
  `_load_merge_report_tie_cutoff(job_id, storage_account)` reads
  `{job_id}/merge-report.json` (bounded, tolerant of missing/malformed) and
  `build_default_alignments_payload` gains an optional `tie_cutoff` field
  (`overflow_count`, `diversity_reserved_count`, optional `max_target_seqs`,
  sample `queries`) emitted only when there is a signal.
- `web/src/api/blast.ts`: new `BlastTieCutoff` interface; `resultsAlignments`
  response gains optional `tie_cutoff`.
- `web/src/pages/blastResults/analytics/DescriptionsTabBody.tsx`: new
  `TieCutoffBadge`.
- `web/src/pages/blastResults/analytics/BlastHitsTable.tsx`: bit-score column
  tooltip gains the effective-search-space note.

No IaC change. All new merge behaviour is opt-in and default-OFF per the
default-OFF guard rule; production scientific output is unchanged by default.

## Validation evidence

- `uv run pytest -q api/tests` → 2559 passed, 3 skipped.
- `uv run pytest api/tests/test_sharded_merge.py -m ''` → 10 passed (incl. the 4
  new deterministic-tie-order and diversity-aware-cutoff tests).
- `uv run pytest -q api/tests/test_job_artifacts.py` → 18 passed (incl. the 5 new
  `_load_merge_report_tie_cutoff` cases: overflow summary, no-signal omission,
  missing report, malformed JSON, diversity-only).
- `uv run ruff check api` → clean.
- `cd web && npm run build` → built; `npx vitest run src/pages/blastResults` →
  116 passed.
