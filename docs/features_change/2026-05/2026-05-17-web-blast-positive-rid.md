# Web BLAST Positive RID Evidence

## Motivation

The search-space default work needs hit-positive NCBI Web BLAST evidence before
claiming Web-compatible result equivalence for sharded execution. The earlier
`core_nt` calibration proved a no-hit statistical case only.

## User-Facing Change

No runtime behavior changed. The discovery document now records the first
hit-positive Web BLAST RID and the corrected NCBI Web database value for 16S
searches.

## API/IaC Diff Summary

- No API changes.
- No IaC changes.
- Added evidence under `docs/temp/web-blast-equivalence/2026-05-17-16s-carnobacterium/`.
- Updated `docs/blast-searchsp-discovery.md` to mark `EQ-06` done for Web
  evidence and to note that the Web 16S database value is
  `rRNA_typestrains/16S_ribosomal_RNA`.

## Validation Evidence

- NCBI Web BLAST RID `0JXX09HH016` for
  `scripts/dev/test_queries/16S_carnobacterium.fa` against
  `rRNA_typestrains/16S_ribosomal_RNA`.
- Retrieved XML evidence reports `hit_count: 500`, `hsp_count: 500`,
  `Statistics_db-len: 40051470`, and `Statistics_db-num: 27648`.
- Negative-control RID `0JXTP4A0014` shows that submitting the local basename
  `16S_ribosomal_RNA` directly to QBlast yields `Statistics_db-len=0` and no
  hits, so the Web form database value must be captured explicitly.