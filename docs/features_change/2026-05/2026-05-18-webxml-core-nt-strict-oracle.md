# 2026-05-18 Web XML Core NT Strict Oracle Evidence

## Motivation

Use a fresh NCBI Web BLAST XML (`outfmt 5`) result as the oracle for `core_nt` sharded equivalence instead of relying on an older CSV export whose HSP values no longer matched the warmed database snapshot.

## User-facing change

No UI behavior changed. The evidence now distinguishes three claims:

- Wide sharded candidate pools contain all Web top-500 hits and matching HSP values.
- Natural deterministic sharded ordering does not reproduce Web's top-N tied-hit order.
- A same-run Web top-500 accession oracle can make the sharded merge exactly match the Web XML-derived CSV fields.

## API/IaC diff summary

- Added `scripts/dev/eq14-core-nt-webxml-sharded.sh`.
- The script submits NCBI Web BLAST for MPXV F3L against `core_nt` with XML output, polls the RID from an AKS system-node Job, generates a normalized CSV from XML, dispatches 10 blastpool shard Jobs, and compares Web XML fields against the sharded widepool and strict oracle merge.

## Validation evidence

- `bash -n scripts/dev/eq14-core-nt-webxml-sharded.sh`
- AKS system-node Job: `elb-equivalence-job-20260518072757`
- Web RID: `0NFW8888016`
- Evidence: `docs/temp/web-blast-equivalence/aks-runner-20260518T072909Z/elb-equivalence-job-20260518072757/`
- Summary: strict Web accession oracle produced `equivalent: true`, `exact_order: true`, `shared_accessions: 500`, and `value_mismatch_count: 0` against the fresh Web XML. The widepool contained all Web accessions and had no HSP value mismatches, but its default top-N order differed from Web.
