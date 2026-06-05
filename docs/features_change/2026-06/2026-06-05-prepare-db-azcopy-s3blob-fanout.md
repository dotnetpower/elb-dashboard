---
title: prepare-db AKS download — single azcopy S3Blob replaces per-file PipeBlob loop
description: The AKS-fanout BLAST DB download now runs one azcopy S3Blob copy per
  shard (native parallelism, idempotent resume) instead of a per-file serial
  curl|PipeBlob loop, and pins azcopy 10.28.0 because the aka.ms build (10.32.4)
  crashes on S3Blob. Proven live at 172 MB/s, 143 MiB peak under the 1Gi cap.
tags:
  - blast
  - infra
---

# prepare-db AKS download: single azcopy S3Blob per shard

## Motivation

Downloading large BLAST databases (`nt`, `core_nt`) through the AKS-fanout
prepare-db path repeatedly failed to converge — a multi-hour loop of
`partial` → re-Get → `partial`. The root cause was structural, not a bug: each
shard pod ran the NCBI files **one at a time** through
`curl <s3> | azcopy copy <blob> --from-to=PipeBlob`. That serial pipeline threw
away azcopy's native multi-file / multi-connection parallelism, PipeBlob has no
per-file resume (a multi-GB volume that dies at 90% restarts from 0), and every
backoff retry re-scanned thousands of already-staged blobs with one
`azcopy list` per file (~12 min/shard of pure scanning). Pod OOM/SNAT resets
then restarted the scan, so the Job could never finish within its deadline.

The sibling [`elastic-blast-azure`](https://github.com/dotnetpower/elastic-blast-azure)
benchmark already recorded the fast path: 82 GB staged S3→Azure Blob in **102 s
at 860 MB/s** with a **single** `azcopy copy` over a glob, not a per-file loop.

## What was proven before changing code

Every design unknown was verified live on `elb-cluster-02` with throwaway pods
(same `mcr.microsoft.com/azure-cli:2.81.0` image, same 1Gi memory cap, kubelet
MI, Storage left `publicNetworkAccess: Disabled`):

- **PE write under Storage Disabled** — a single `azcopy --from-to=S3Blob` wrote
  16S (12 files) through the private endpoint via the kubelet MI; 0 failed.
- **Throughput + memory** — 5×3 GB nt volumes (15 GB) in one azcopy call:
  **172.3 MB/s, 0 failed, peak RSS 143 MiB** (14 % of the 1Gi cap, no OOM).
- **Anonymous S3** — azcopy enumerates the public NCBI bucket with no AWS
  credentials.
- **azcopy version crash** — the current `aka.ms/downloadazcopy-v10-linux`
  serves **10.32.4, which panics on every `--from-to=S3Blob` copy**
  (`getSourceServiceClient` nil deref). **10.28.0** handles S3Blob correctly.
- **Layout** — trailing `/*` source + `--include-pattern` lands files FLAT at
  `blast-db/<db>/<file>`; azcopy's list-of-files mode nests `<snapshot>/` and
  breaks the elastic-blast layout, so it is not used.
- **Pattern scale** — a 480-name include-pattern (the realistic per-shard size
  for `nt`) matched exactly 480 files.

## User-facing change

Large BLAST DB downloads via the Storage card (`Get` / `Update`, `mode=aks`)
now transfer each shard with one azcopy S3Blob copy. They are dramatically
faster and converge reliably; an interrupted run heals itself on the next click
(`--overwrite=true` re-fetches, and azcopy commits each blob atomically so no
truncated/0-byte blob is ever left behind). No UI change.

## API / IaC diff summary

No IaC change; no manifest change (the `ELB_SOURCE_VERSION` env the new script
needs was already present). The change is the baked pod script plus its tests:

- `api/services/k8s/prepare_db_jobs.py` — `PREPARE_DB_AKS_SCRIPT` rewritten:
  - azcopy bootstrap pinned to **10.28.0** from GitHub releases (override via
    `ELB_AZCOPY_URL`); `aka.ms` removed (serves the crashing 10.32.4).
  - Per-file `curl|PipeBlob` loop, `blob_content_length` helper, `VERIFY_EVERY_N`
    pre-flight/verify, and the size-mismatch delete logic all removed.
  - Each shard builds an `--include-pattern` from its shard-file basenames
    (python3; the image ships no GNU text tools) and runs one
    `azcopy copy "${NCBI_BASE}/${SOURCE_VERSION}/*" "${DEST_BASE}/"
    --from-to=S3Blob --include-pattern <names> --block-size-mb=64
    --overwrite=true`. Shard planning, ConfigMap, Indexed Job manifest, and the
    blob-count progress reporter are unchanged.
- `api/tests/test_prepare_db_aks_manifest.py` — replaced the PipeBlob/verify/
  loop guards with new guards for the S3Blob path (single azcopy, include-pattern
  from basenames, flat layout, `--overwrite=true`, azcopy 10.28.0 pin, no awk,
  no list-of-files); removed the now-dead `blob_content_length` / verify-tolerance
  / fd-3-loop tests.

## Validation evidence

- `uv run ruff check api/services/k8s/prepare_db_jobs.py api/tests/test_prepare_db_aks_manifest.py`
  → clean.
- `uv run pytest -q api/tests -k "prepare_db or k8s or warmup"` → 368 passed.
- Live proof pods on `elb-cluster-02` (see "What was proven" above):
  172.3 MB/s, peak 143 MiB, 0 failed, Storage stayed Disabled.
- End-to-end `nt` download monitored from the dashboard after redeploy.

## Notes

- Redeploy of the api/worker images is required because the script is baked into
  the worker image (the ConfigMap is built at dispatch time from
  `PREPARE_DB_AKS_SCRIPT`). This is the sanctioned redeploy exception in
  charter §13 — the change is a baked pod script that cannot be exercised by
  pytest or local uvicorn.
- `--overwrite=true` heals the corrupt/partial `nt` left by the old PipeBlob
  runs by re-fetching every file; `ifSourceNewer` would have skipped the stale
  bad blobs (their dest LMT post-dates the NCBI snapshot).
