---
title: outfmt 7 results populate Description and HSP Cover columns
description: The "Include taxonomy columns" toggle now emits stitle + qcovs, and the tabular parser maps BLAST's "% query coverage per subject" column, so a tabular outfmt 7 run renders Description, Scientific Name, and HSP Cover instead of leaving them blank.
tags:
  - blast
  - user-guide
---

# outfmt 7 results populate Description and HSP Cover columns

## Motivation

A sharded `core_nt` run submitted with the **Include taxonomy columns** toggle
returned 1,000 hits, but the **Description** and **HSP Cover** columns of the
Descriptions table were blank, and the issue-#32 reason banner stayed up.

Live investigation on the dev cluster (`elb-openapi` 4.24) traced this to two
gaps:

1. The toggle emitted `-outfmt 7 std staxids sscinames`. That specifier carries
   no `stitle` (Description) and no `qcovs` (HSP Cover), so those columns were
   *necessarily* blank — only Scientific Name + taxid were present.
2. Even after manually appending `qcovs` to the specifier, HSP Cover stayed
   blank. The real BLAST+ 2.17.0 `# Fields:` header writes the qcovs column as
   `% query coverage per subject`, and the tabular parser had **no label alias**
   for it, so the column was named `%_query_coverage_per_subject` and the UI's
   `hit.qcovs` lookup silently missed.

The actual header captured from a shard result during the live run:

```
# Fields: query acc.ver, subject acc.ver, % identity, alignment length,
mismatches, gap opens, q. start, q. end, s. start, s. end, evalue, bit score,
subject tax ids, subject sci names, subject title, % query coverage per subject
```

## User-facing change

- The Algorithm Parameters toggle is renamed **Include taxonomy & description
  columns (taxid, scientific name, title, coverage)** and now emits
  `-outfmt 7 std staxids sscinames stitle qcovs`. A single click populates
  **Description**, **Scientific Name**, **taxid**, and **HSP Cover** on a
  tabular (outfmt 7) run.
- The HSP Cover column now shows BLAST's reported per-subject query coverage
  (`qcovs`, the same value as NCBI Web BLAST's *Query Cover*) when the run
  carries it, falling back to the per-HSP coordinate estimate
  (`qstart / qend / qlen`) only when `qcovs` is absent. The column tooltip was
  updated to describe both sources.
- The issue-#32 "columns blank" banner now points users at the toggle and the
  correct `stitle qcovs` specifier (it previously suggested `stitle qlen`).

## API / IaC diff summary

- `api/services/blast/results_parser.py`: add `_FIELD_LABEL_TO_COLUMN` aliases
  for `% query coverage per subject` → `qcovs`, `% query coverage per hsp` →
  `qcovhsp`, `% query coverage per uniq subject` → `qcovus`; coerce the three
  coverage columns to float for a single numeric convention across the parsed
  and computed code paths.
- `api/services/blast/result_analytics.py`: `annotate_result_hit` now preserves
  a qcovs (and scovs) value the run reported directly instead of always
  overwriting it with the weaker per-HSP coordinate estimate.
- `web/src/pages/blastSubmit/useSubmitMutation.ts`: taxonomy toggle emits
  `7 std staxids sscinames stitle qcovs`.
- `web/src/pages/blastSubmit/AlgorithmParametersSection.tsx`: toggle label + tip
  updated.
- `web/src/pages/blastResults/analytics/BlastHitsTable.tsx`: banner text + HSP
  Cover tooltip updated.
- No IaC change. No sidecar / terminal / elb-openapi change — result parsing
  happens in the dashboard `api` sidecar, so only the `api` + `frontend`
  sidecars are affected.

## Validation evidence

- Live, on the dev cluster (`elb-cluster-01`, `elb-openapi` 4.24): a sharded
  `core_nt` MPXV F3L run submitted with `7 std staxids sscinames stitle qcovs`
  completed (1,000 hits, 10 shards) with **Description** populated
  ("Monkeypox virus isolate …, complete genome"), **Scientific Name** =
  "Monkeypox virus", and the per-shard result file confirmed to carry the
  `% query coverage per subject` column.
- `api/tests/test_blast_results_parser.py::test_parse_outfmt7_maps_taxonomy_and_coverage_columns`
  locks the qcovs label mapping + float coercion + stitle comma preservation.
- `api/tests/test_blast_result_analytics_organism.py::test_annotate_preserves_reported_qcovs_over_computed`
  and `::test_annotate_computes_qcovs_when_absent` lock the annotate guard.
- `web/src/pages/blastSubmit/taxonomyOutfmt.test.ts` updated to assert the
  extended specifier.
- `uv run pytest -q api/tests -k "blast or shard or merge or result or analytics or tabular"`
  → 1147 passed, 3 skipped. `cd web && npm run build` → green.
