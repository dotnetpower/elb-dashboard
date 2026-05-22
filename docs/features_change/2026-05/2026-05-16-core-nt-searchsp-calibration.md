# Plan: core_nt Full-Database Search Space Calibration

## Motivation

Small custom-subject probes showed that BLAST effective search space is dependent
on the query, database, and options. For a production-sized database such as
`core_nt`, the sharded ElasticBLAST baseline should therefore come from a local
full-database BLAST+ run, not from a fixed Web BLAST assumption.

## User-Facing Change

- Added a `Large DB / core_nt Calibration Strategy` section to
  [docs/research/blast-searchsp-discovery.md](../../research/blast-searchsp-discovery.md).
- Added [scripts/dev/core-nt-searchsp-calibration.sh](../../../scripts/dev/core-nt-searchsp-calibration.sh),
  a guarded helper for planning, creating, remotely running, fetching results
  from, inspecting, and deleting a temporary Azure VM used for the one-off full
  DB calibration experiment. VM-side commands can be printed by `vm-runbook` or
  executed over SSH by `remote-calibrate` after an explicit approval gate.
- The VM runbook downloads `core_nt` tarballs with parallel resumable `curl`
  workers controlled by `CORE_NT_DOWNLOAD_JOBS` and skips already complete
  tarballs after `tar -tzf` validation.

## API / IaC Diff Summary

- No API route changes.
- No frontend changes.
- No Bicep/IaC changes.
- The helper script uses Azure CLI directly only after explicit local approval
  gates and does not run during deployment or CI.

## Validation Evidence

- `bash -n scripts/dev/core-nt-searchsp-calibration.sh`
- `scripts/dev/core-nt-searchsp-calibration.sh plan --rg rg-elb-core-nt-searchsp-20260516 --location koreacentral --vm-size Standard_E96as_v5`
- `scripts/dev/core-nt-searchsp-calibration.sh vm-runbook --rg rg-elb-core-nt-searchsp-20260516`
- `scripts/dev/core-nt-searchsp-calibration.sh vm-runbook --rg rg-elb-core-nt-searchsp-20260516 | grep -E 'curl|CORE_NT_DOWNLOAD_JOBS|FORMAT_CORE_NT_DATA_DISK|word_size 28|RUN_SEARCHSP1'`
- First VM smoke run installed packages and mounted/formatted the data disk, then
  exposed a missing runtime library: BLAST+ 2.17.0 requires `libgomp.so.1`.
  Added `libgomp1` to the VM package list before rerunning calibration.
- Temporary Azure VM run:
  - Resource group: `rg-elb-core-nt-searchsp-20260516`
  - VM: `vm-elb-core-nt-searchsp`
  - Region: `koreacentral`
  - Size: `Standard_E96as_v5`
  - Data disk: 1 TiB mounted at `/mnt/elb-calibration`
- Download validation:
  - NCBI `core_nt` tarballs: `88/88` validated with `tar -tzf`
  - Downloader: parallel resumable `curl` with `CORE_NT_DOWNLOAD_JOBS=6`
  - Completion markers: `complete_markers=88/88`, log entries `done=63`, `skip=25`
- BLAST baseline evidence from `docs/temp/core-nt-searchsp/core_nt-searchsp-calibration-results.tgz`:
  - `blastn: 2.17.0+`, package `blast 2.17.0`, build `Jul 1 2025 08:59:18`
  - Options: `-word_size 28 -dust yes -evalue 10 -max_target_seqs 500 -outfmt 5`
  - Threads: `96`
  - Query SHA-256: `4c7007e3431bb780ab769516c1a90cc0604dedb9d7e9e9b3e633aa7ac2ea4c51`
  - Database: `125,619,662` sequences; `1,041,443,571,674` total bases; BLASTDB version `5`; date `May 2, 2026 1:17 AM`
  - Full-database `Statistics_eff-space`: `32156241807668`
  - `blastn` exit status: `0`
  - Wall-clock runtime for the baseline query: `0:44.79`
- Fetched archive locally:
  - `RESULT_DIR=docs/temp/core-nt-searchsp scripts/dev/core-nt-searchsp-calibration.sh fetch-results --rg rg-elb-core-nt-searchsp-20260516 --vm-name vm-elb-core-nt-searchsp`
- Cleanup evidence:
  - `ELB_CORE_NT_DELETE=delete-rg-elb-core-nt-searchsp-20260516 scripts/dev/core-nt-searchsp-calibration.sh delete --rg rg-elb-core-nt-searchsp-20260516 --confirm-resource-group rg-elb-core-nt-searchsp-20260516`
  - `az group wait --name rg-elb-core-nt-searchsp-20260516 --deleted`
  - `az group exists --name rg-elb-core-nt-searchsp-20260516` returned `false`
- `git diff --check -- docs/blast-searchsp-discovery.md scripts/dev/core-nt-searchsp-calibration.sh docs/features_change/2026-05/2026-05-16-core-nt-searchsp-calibration.md`

The destructive cleanup path requires
`ELB_CORE_NT_DELETE=delete-<resource-group-name>` and
`--confirm-resource-group <resource-group-name>` before it runs `az group delete`.
The remote calibration path requires `ELB_CORE_NT_REMOTE_APPROVED=1` before it
formats the throwaway data disk over SSH.