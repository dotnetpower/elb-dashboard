# Results file grouping

## Motivation

The job Results section listed every blob in one flat table. Split-result manifests, merge reports, pod status files, and logs appeared beside the actual BLAST output, making completed jobs look noisy and hard to scan.

## User-facing change

The Results section now groups files by purpose:

- Primary outputs: BLAST result files such as `.out`, `.out.gz`, `.xml`, `.xml.gz`, and `.asn`.
- Reports and manifests: JSON/report/manifest artifacts in a collapsed section when primary outputs exist.
- Diagnostic logs: pod/job/status/log files in a collapsed section unless diagnostics are the only files available.

When no primary output exists, the most useful available artifact group is still shown so the user is not left with an empty panel.

## API/IaC diff summary

- No API or IaC changes.
- `splitBlastResultFiles` now classifies result blobs into primary, support, and diagnostic groups.
- `BlastResultsTable` renders grouped sections with compact headers and keeps secondary artifacts collapsed by default.

## Validation evidence

- `cd web && npm run test -- blastResultsModel.test.ts`
- `cd web && npm run build`
