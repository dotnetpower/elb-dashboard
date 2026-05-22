# Discovery: NCBI BLASTAlign Search Space Is Not Fixed

## Motivation

Precise sharded BLAST needs every shard to use the same statistical search space
as the full/reference run. We needed to test whether NCBI Web BLASTAlign uses a
fixed hidden `searchsp` for custom subject databases.

## User-Facing Change

- Added [docs/research/blast-searchsp-discovery.md](../../research/blast-searchsp-discovery.md), a
  reproducible discovery note explaining how Web/default `searchsp` was inferred.
- Added [scripts/dev/ncbi-searchsp-discovery.py](../../../scripts/dev/ncbi-searchsp-discovery.py),
  a manual dev probe that submits small NCBI Web BLASTAlign jobs and compares
  them with local BLAST+ runs through the terminal image.

## API / IaC Diff Summary

- No API route changes.
- No frontend changes.
- No IaC changes.

## Validation Evidence

Manual Web BLASTAlign + local BLAST+ probes on 2026-05-16:

| Case | RID | Inferred `searchsp` | Local default equals Web | Local explicit `-searchsp` equals Web |
| --- | --- | ---: | --- | --- |
| `baseline_32nt_4_subjects` | `0G0J7RM8114` | 2704 | yes | yes |
| `longer_64nt_4_subjects` | `0G0PF0XD114` | 12544 | yes | yes |
| `wider_32nt_8_subjects` | `0G0US0KB114` | 4950 | yes | yes |

Conclusion: the value is data/options dependent, not fixed.