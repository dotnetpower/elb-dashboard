# 2026-05-16 — E16 shard warmup searchsp comparison

## Motivation

After the full local `core_nt` BLAST+ calibration measured
`Statistics_eff-space = 32156241807668`, the next check was to verify the value
on a real AKS warmup/sharded path. The requested shape was E16 × 10. The exact
`Standard_E16s_v5 × 10` shape was blocked by quota in `koreacentral`, so the
experiment used `Standard_E16s_v3 × 10`, which has the same 16 vCPU / 128 GiB
node class and fit the available ESv3 quota.

## User-Facing Change

No product UI or API behavior changed. This is operational validation evidence
for the shard-wide `-searchsp` value used by precise ElasticBLAST sharding.

## API / IaC Diff Summary

- No API route changes.
- No frontend changes.
- No Bicep/IaC changes.
- Temporary AKS node pool only:
  - Added `blastp16v3` on `rg-elb-01/elb-cluster` with `10 × Standard_E16s_v3`.
  - Labelled nodes with `workload=blast` and `searchsp-ordinal=00..09`.
  - Deleted the pool after evidence collection.

## Validation Evidence

### Quota and setup

- Existing cluster: `rg-elb-01 / elb-cluster`, region `koreacentral`.
- Existing blast pool before/after cleanup: `blastpool`, `3 × Standard_E32s_v5`.
- `Standard_E16s_v5 × 10` was not possible: `Standard ESv5 Family vCPUs` quota
  was `96/100` in use, while E16s_v5 × 10 requires `160` family vCPUs.
- Feasible substitute: `Standard_E16s_v3 × 10`; ESv3 quota was `32/200` before
  the temporary pool and regional quota was sufficient.

### Storage/shard preparation

`api.services.db_sharding.ensure_shard_sets(get_credential(), "elbstg01", "core_nt")`
returned:

```text
{
  'db_name': 'core_nt',
  'total_volumes': 88,
  'total_bytes': 292365689731,
  'total_letters': 1041443571674,
  'total_sequences': 125619662,
  'bytes_to_cache': 263930372302,
  'bytes_total': 304539057462,
  'shard_sets': [1, 2, 3, 4, 5, 6, 8, 10],
  'created': 78,
  'skipped': 0,
  'errors': []
}
```

### Actual E16 warmup

Ten Kubernetes Jobs, one per E16 node, ran `/scripts/init-db-shard-aks.sh` and
then `/scripts/blast-vmtouch-aks.sh` against `core_nt_shard_00..09`.

Summary:

| Shard | Download runtime | Local size | Sequences | Bases |
| --- | ---: | ---: | ---: | ---: |
| `00` | 37 s | 36 GiB | 12,960,152 | 107,137,923,018 |
| `01` | 42 s | 36 GiB | 12,969,497 | 107,117,367,948 |
| `02` | 38 s | 36 GiB | 12,854,018 | 107,142,899,143 |
| `03` | 38 s | 36 GiB | 12,747,873 | 107,118,346,540 |
| `04` | 38 s | 36 GiB | 12,875,586 | 107,131,047,981 |
| `05` | 38 s | 36 GiB | 13,045,521 | 107,132,833,789 |
| `06` | 47 s | 36 GiB | 13,090,540 | 107,113,831,974 |
| `07` | 36 s | 36 GiB | 12,810,654 | 107,145,974,380 |
| `08` | 36 s | 36 GiB | 12,979,148 | 107,131,538,110 |
| `09` | 30 s | 28 GiB | 9,286,673 | 77,271,808,791 |

Shard totals matched the full DB calibration:

```text
sum_db_len = 1041443571674
sum_db_num = 125619662
```

### Search space comparison

Each warmed shard ran the 64 nt calibration query twice:

1. Default shard-local BLAST+ options.
2. The same options plus `-searchsp 32156241807668`.

| Shard | Default shard `Statistics_eff-space` | With full DB `-searchsp` |
| --- | ---: | ---: |
| `00` | 3629470027572 | 32156241807668 |
| `01` | 3628761623292 | 32156241807668 |
| `02` | 3629747472502 | 32156241807668 |
| `03` | 3629020951900 | 32156241807668 |
| `04` | 3629322533634 | 32156241807668 |
| `05` | 3629209917406 | 32156241807668 |
| `06` | 3628517936316 | 32156241807668 |
| `07` | 3629896261840 | 32156241807668 |
| `08` | 3629233564780 | 32156241807668 |
| `09` | 2617769092434 | 32156241807668 |

Result: `all_full_searchsp_equal = True`.

This confirms the operational rule: shard-local defaults are smaller and differ
by shard, but passing the calibrated full DB value forces every warmed shard to
use the same search space as the full DB baseline.

### Cleanup

- Deleted Kubernetes Jobs labelled `app=core-nt-e16-warm` and
  `app=core-nt-e16-compare`.
- Deleted temporary node pool `blastp16v3`.
- Verified final node pools:

```text
systempool  Standard_D2s_v3   1  Succeeded
blastpool   Standard_E32s_v5  3  Succeeded
```

- Verified no `blastp16v3` nodes remained: `0`.
