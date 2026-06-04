---
title: Byte-based prepare-db copy ETA
description: Replace the misleading file-count ETA on the AKS-fanout prepare-db copy with a byte-based projection driven by the trailing-window throughput.
tags:
  - blast
  - ui
---

# Byte-based prepare-db copy ETA

## Motivation

The BLAST DB row rendered a remaining-time estimate by extrapolating the
**file count**: `elapsed × (total − copied) / copied`. For the AKS-fanout
download path this is badly wrong:

- A re-run finds thousands of small blobs already staged under `<db>/`, so the
  staged-blob count (`copy_status.success`) jumps to a large value instantly.
  The count rate is then enormous and the ETA collapses to a bogus "~12s left".
- The handful of remaining files are the largest `.nsq` sequence volumes
  (~3 GB each, ~486 per shard for `nt`), each taking many minutes, so linear
  file-count extrapolation underestimates by orders of magnitude.

Observed live: "Copying 4814 / 4874 files · 941s · 37.0 MB/s · **~12s left**"
while every shard was still streaming multi-GB volumes (real ETA ≈ tens of
minutes).

## User-facing change

The AKS-fanout copy now shows a **byte-based ETA** computed from the bytes
still to land over the recent (trailing-window) download throughput:
`(bytes_total − bytes_done) / windowed_bytes_per_sec`. The trailing rate
reflects only recent movement, so it is immune to the staged-blob startup
inflation. When the rate is momentarily unavailable (azcopy `--from-to=PipeBlob`
commits a blob only when a whole file finishes), the existing
"transferring large volumes…" note covers the gap instead of a misleading
count-based figure. The server-side blob-to-blob path (no byte totals) keeps the
file-count `formatEta` fallback unchanged.

## API / IaC diff summary

- **Backend** [api/tasks/storage/prepare_db_via_aks.py](../../../api/tasks/storage/prepare_db_via_aks.py):
  compute `total_bytes_expected = sum(file_sizes)` and emit it as
  `copy_status.bytes_total` from `_on_job_progress` (only when > 0).
- **Frontend** [web/src/components/cards/storage/blastDbProgress.ts](../../../web/src/components/cards/storage/blastDbProgress.ts):
  new pure helpers `computeWindowedBytesPerSec` (numeric trailing-window rate)
  and `formatEtaFromBytes` (remaining-bytes / rate → `"~28m left"`).
- **Frontend** [web/src/components/cards/storage/BlastDbRow.tsx](../../../web/src/components/cards/storage/BlastDbRow.tsx):
  derive `bytesTotal`, compute `byteEtaLabel` in the sampling effect, and prefer
  it over the count-based `formatEta` whenever `bytes_total` is present.
- **Types**: `copy_status.bytes_total?: number` added to
  [web/src/api/blast.ts](../../../web/src/api/blast.ts) and
  [web/src/components/cards/storage/useBlastDb.ts](../../../web/src/components/cards/storage/useBlastDb.ts).

No IaC change.

## Validation evidence

- `uv run pytest -q api/tests/test_prepare_db_aks_task.py` → 11 passed
  (new `bytes_total` present / omitted assertions).
- `npx vitest run …/blastDbProgress.test.ts` → 27 passed (new
  `computeWindowedBytesPerSec` + `formatEtaFromBytes` cases).
- `uv run ruff check` on the touched backend files → clean.
- `cd web && npm run build` → built successfully.
- Live `nt` download confirmed healthy during the change: all 10 shard pods
  Running, RESTARTS 0, each mid-copy on ~3 GB `.nsq` volumes (file ~10/487).
