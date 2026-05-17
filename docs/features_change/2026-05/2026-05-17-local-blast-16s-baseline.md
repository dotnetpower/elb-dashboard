# Local BLAST 16S Baseline Evidence

## Motivation

The AKS one-shot pod path was too slow for the small `EQ-07` Web-vs-local 16S
baseline check because pod scheduling and image readiness dominated the work.
Installing BLAST+ locally makes the small database comparison fast enough to
iterate.

## User-Facing Change

No application behavior changed. The discovery evidence now records the local
BLAST+ installation path and the first local 16S baseline comparison against the
saved NCBI Web BLAST XML.

## API/IaC Diff Summary

- No API changes.
- No IaC changes.
- Installed BLAST+ 2.17.0 under `~/.local/elb-tools/ncbi-blast-2.17.0+`.
- Downloaded the local `16S_ribosomal_RNA` BLAST database under
  `~/.cache/elb-dashboard/blastdb/16S_ribosomal_RNA/`.
- Added evidence files under
  `docs/temp/web-blast-equivalence/2026-05-17-16s-carnobacterium/`.

## Validation Evidence

- `blastn -version` reports `BLASTN 2.17.0+`.
- `blastdbcmd -db 16S_ribosomal_RNA -info` reports `27,648 sequences` and
  `40,051,470 total bases`, matching the saved Web XML statistics.
- Local full baseline command completed in `2.54s` and produced 500 hits / 500
  HSPs with the same top hit as Web RID `0JXX09HH016`.
- `scripts/dev/compare-blast-xml.py` generated
  `web-vs-local-16s-canonical-compare.json`; strict comparison does not pass yet
  (`difference_count=5991`). The first hit-order mismatch is rank 111, Web/local
  hit ID overlap is 438/500, and Web XML reports `Statistics_eff-space=0` while
  local BLAST+ reports `57425628120`.
- A synthetic local 4-shard 16S probe completed without AKS: FASTA extraction was
  `1.81s`, shard BLAST runs were `0.93s` to `1.06s` each, and merged XML matched
  the full local baseline statistics exactly (`db_len=40051470`, `db_num=27648`,
  `eff_space=57425628120`, `hsp_len=26`). Strict hit ordering still does not
  pass: contiguous shards preserve the same top accession and top-10 order, but
  first accession mismatch is rank 15 and top-500 accession overlap is 442/500.
- `db-snapshot-and-value-equivalence.json` confirms the important same-database
  condition for the 16S probe: Web XML and the local May 14 2026 database both
  report `db_len=40051470` and `db_num=27648`. Web-vs-local full has 438 shared
  accessions and all 438 have identical primary HSP/value fields. Local
  full-vs-sharded has 442 shared accessions; 437 have identical primary HSP/value
  fields, and the five mismatches are `Hit_def` GI-formatting differences caused
  by FASTA-based shard DB regeneration.