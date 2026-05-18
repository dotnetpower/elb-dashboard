# 2026-05-18 Core NT EQ-13 Widepool Validation

## Motivation

Validate Web BLAST equivalence on `core_nt` instead of treating small marker databases as production sharding proof.

## User-facing change

No UI behavior changed. The validation evidence clarifies that 16S/18S/ITS checks are small-database snapshot/form-value diagnostics, while sharded Web equivalence must be proven on `core_nt`.

## API/IaC diff summary

- Added a reusable AKS system-node EQ-13 runner script for `core_nt` MPXV F3L widepool validation.
- The runner dispatches 10 blastpool child Jobs, uses Web-compatible `blastn` options for the F3L probe, captures raw score in outfmt 6, and writes strict Web top-500 oracle diagnostics.
- Fixed the runner's AzCopy download path handling so Storage prefix downloads work whether AzCopy creates the run-id directory or writes directly into the destination.

## Validation evidence

- `bash -n scripts/dev/eq13-core-nt-f3l-widepool.sh`
- AKS system-node Job: `elb-equivalence-job-20260518070958`
- Evidence: `docs/temp/web-blast-equivalence/aks-runner-20260518T071101Z/elb-equivalence-job-20260518070958/`
- Summary: Web CSV top-500 accessions were all present in the `core_nt` widepool and strict oracle ordering matched, but HSP value fields did not match this CSV (`value_mismatch_count: 500`), so the provided CSV is not a complete Web-equivalence oracle for the current `core_nt` snapshot.
