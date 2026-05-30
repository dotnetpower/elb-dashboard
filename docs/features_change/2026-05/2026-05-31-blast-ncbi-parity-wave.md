---
title: NCBI Web BLAST parity wave — 8 code fixes + 12 documented gaps
description: First wave of explicit parity work between this control plane's BLAST experience and the NCBI Web BLAST baseline. Ships qframe/sframe extraction, an alignment Copy/Download FASTA action, a Descriptions Frame badge, a Charter §9 no-SAS regression test, and an expanded submit-option whitelist; documents the remaining 12 gaps with severity + cost.
tags:
  - blast
  - user-guide
---

# NCBI Web BLAST parity wave — 8 code fixes + 12 documented gaps

Date: 2026-05-31

## Motivation

A focused audit of the BLAST submit / result experience against
<https://blast.ncbi.nlm.nih.gov/Blast.cgi> catalogued 20 concrete gaps. Some
were silent scientific data loss (reading frames dropped from translated BLAST
output), some were UX gaps (no Copy / Download FASTA on the Alignment viewer,
no Frame indicator on the Descriptions table), and some were Charter §9
hardening gaps with no regression test guarding them. The longer list mixes
"cheap to ship" with "needs a new tree-builder library" — the wave deliberately
ships only the cheap-and-impactful items and documents the rest so a future
session can pick them up without re-doing the audit.

## User-facing change

### Translated programs now show reading frames

`blastx`, `tblastn`, and `tblastx` runs surface `Hsp_query-frame` /
`Hsp_hit-frame` from BLAST XML and the `query frame` / `subject frame` columns
from `-outfmt 6 / 7` tabular output. The Descriptions table renders a Frame
badge `(qframe/sframe)` next to the Query Range column and the Alignment viewer
footer shows `Query frame: N · Subject frame: N` when the program actually
emitted them. Nucleotide-only programs (blastn, blastp) that emit `0` are
filtered out so users do not see a misleading "Frame: 0".

### Alignment viewer Copy / Download FASTA

The Alignment viewer gained three buttons mirroring NCBI's per-hit actions:

- **Copy alignment** — copies a pairwise text block in NCBI's Query/Sbjct
  positional layout, wrapped at 60 columns.
- **Copy FASTA** — copies a two-record FASTA (query + subject) with gaps
  stripped.
- **Download FASTA** — downloads the same two-record FASTA as
  `<qseqid>__<sseqid>.fasta`, with non-alphanumeric characters sanitised to
  underscores for cross-OS safety.

Each action shows a 2 s feedback message inline so screen readers and visual
users both see confirmation.

### NCBI Advanced submit flags accepted via OpenAPI today

The BLAST submit option whitelist `_BLAST_SUBMIT_OPTION_KEYS` grew from ~40 to
~60 keys, adding every algorithm / filter / output flag the NCBI Web BLAST
Advanced submit form exposes: `matrix`, `threshold`, `comp_based_stats`,
`culling_limit`, `best_hit_overhang`, `best_hit_score_edge`, `qcov_hsp_perc`,
`perc_identity`, `gilist`, `negative_gilist`, `seqidlist`, `xdrop_gap`,
`xdrop_gap_final`, `xdrop_ungap`, `window_size`, `parse_deflines`,
`soft_masking`, `lcase_masking`, `ungapped`, `num_alignments`,
`num_descriptions`. OpenAPI callers can use these today via the top-level
submit body or the `options` sub-dict. UI controls (dropdowns / sliders) for
these flags are deferred to a follow-up wave and tracked in the Stage 11 plan
matrix.

### Charter §9 regression test

A new regression test (`test_blast_results_routes_never_emit_sas_tokens`)
walks the JSON responses from every BLAST result route the browser actually
consumes (`/results`, `/results/aggregate`, `/results/alignments`) and asserts
that no Storage SAS marker (`?sig=`, `&sig=`, `?sv=`, `skoid=`, `sktid=`,
`AccountKey=`, …) ever appears. This locks in the existing
"no SAS tokens in the browser" contract so any future regression that
re-introduces `generate_blob_sas` or returns a raw signed URL fails loudly
here instead of in production telemetry.

## API / IaC diff summary

| Surface | Change |
| --- | --- |
| `api/services/blast/submit_payload.py` | Whitelist grew from ~40 to ~60 keys (NCBI Advanced flags). |
| `api/services/blast/results_parser.py` | `qframe` / `sframe` added to `_INT_COLUMNS`; `query frame` / `subject frame` / `frame` added to `_FIELD_LABEL_TO_COLUMN`; `_build_hit_row` extracts `<Hsp_query-frame>` and `<Hsp_hit-frame>` and drops `0`. |
| `web/src/api/blast.ts` | `BlastHit` interface gained optional `qframe` / `sframe` fields. |
| `web/src/pages/blastResults/analytics/AlignmentViewer.tsx` | Footer renders Query frame / Subject frame; new `AlignmentExportActions` component with Copy alignment / Copy FASTA / Download FASTA; new helpers `buildPairwiseAlignmentText`, `buildAlignmentFasta`, `wrapFasta`. |
| `web/src/pages/blastResults/analytics/BlastHitsTable.tsx` | Conditional Frame badge `(qframe/sframe)` next to Query Range column with tooltip. |
| `api/tests/test_blast_submit_route_options.py` | `+2` tests covering the new whitelist via top-level body and `options` sub-dict. |
| `api/tests/test_blast_results_parser.py` | `+3` tests covering `qframe` / `sframe` extraction (XML + tabular) and zero-drop. |
| `api/tests/test_blast_results_routes.py` | `+2` tests covering the no-SAS contract on every browser-facing result route. |
| `docs/research/web-blast-compatibility-plan.md` | New Stage 11 section with the 20-gap matrix, Critique Hardening Loop pass 1, and updated Stage Progress Board entry. |

No infrastructure (Bicep / Container App template) changes.

## Validation evidence

```
uv run pytest -q api/tests
2225 passed, 3 skipped in 33.85s
# baseline 2218 → +7 new tests, no regressions

uv run ruff check api
All checks passed!

cd web && npm test -- --run
Test Files  56 passed (56)
     Tests  433 passed (433)

cd web && npm run build
✓ built in 8.07s
# existing large-chunk warning unchanged
```

No redeployment was performed (Charter §13 "Do NOT redeploy for ordinary code
changes"): the changes are entirely backend `api/`, frontend `web/`, and docs
— no sidecar layout, no Container App template, no Bicep, no
`terminal/Dockerfile*`, no `exec_server.py`.

## Stage 11 plan-doc critique log

The full 20-gap matrix (severity + status + cost estimate per item) and the
Critique Hardening Loop pass 1 record live in
[docs/research/web-blast-compatibility-plan.md](../../research/web-blast-compatibility-plan.md)
under the new "Stage 11: NCBI Web BLAST Parity Wave (2026-05-31)" section.
The 12 remaining items are split across:

- 4 Planned Medium items (NCBI 5-band bit-score legend on Graphic Summary,
  Distance Tree tab, Karlin–Altschul header parameters, Reformat results
  re-render route).
- 4 Planned UI-only items for whitelist entries 1–5 (matrix dropdown,
  comp_based_stats dropdown, qcov_hsp_perc / perc_identity sliders, etc.).
- 4 Planned Low items (multi-query effective search space, masking defaults
  doc surface, phase-aware cancel, result pagination, FASTA log
  sanitisation, MSA viewer deep-link).

Wave 2 is scheduled to cover the UI controls for whitelist entries 1–5 plus
the four Planned Medium items.
