---
title: dfs-backed result listing (ADLS Gen2 get_paths) with Blob fallback — flag-gated
description: list_result_blobs lists via the native dfs directory walk when STORAGE_DFS_ENABLED is on, returning the same row shape as the Blob path and falling back to Blob on any dfs error.
tags:
  - storage
  - performance
---

# dfs-backed result listing — flag-gated

Epic #64, issue #68. Builds on the dfs client pool (#65) and the stored prefix
(#66/#67).

## Motivation

On the HNS account, listing a job's results via the native dfs `get_paths`
directory walk is genuinely hierarchical (and bounds enumeration for the
date-tiered layout), whereas the Blob `list_blobs(name_starts_with=...)` is a
flat prefix scan. This wires the dfs listing behind `STORAGE_DFS_ENABLED` so it
can be adopted without changing any caller or the frontend.

## Scope decision (honest)

Only the **listing** path moved to dfs. Single-blob reads
(`read_blob_text`, `read_result_blob_text`, `stream_blob_bytes`, metadata reads)
**stay on the Blob API**: on an HNS account a `DataLakeFileClient` download is the
same bytes from the same storage with zero benefit, and reimplementing the
proven range / gzip-inflation / HTTP-416 / streaming-semaphore logic on a second
SDK would be pure risk. The genuine dfs win for those paths is recursive
directory delete/rename, which is issue #69.

## What landed

- `api/services/storage/dfs_io.py` (new) — `list_paths_dfs` lists files under a
  directory prefix via `FileSystemClient.get_paths(recursive=True)`, returns the
  SAME row shape as `blob_io.list_result_blobs`
  (`file_id`/`name`/`size`/`last_modified`), filters out directory entries
  (Blob yields only blobs), degrades a missing directory to `[]`, and normalizes
  `last_modified` to a string.
- `api/services/storage/blob_io.py` — `list_result_blobs` dispatches to
  `list_paths_dfs` when `dfs_enabled()`, and **falls back to the Blob prefix
  scan on any dfs error** so a transient dfs issue never breaks result listing.

## Validation evidence

- `uv run pytest api/tests/test_storage_dfs_io.py` → **8 passed** (row-shape +
  directory filter, last_modified normalization, missing-dir → `[]`, trailing
  slash stripped, limit honoured, dispatch flag-off/on, dfs-error → Blob
  fallback).
- Flag-OFF regression: `test_storage_data` → **39 passed** (Blob listing
  unchanged).
- Flag-ON smoke: `STORAGE_DFS_ENABLED=true pytest test_blast_results_routes` →
  **40 passed** (with no real dfs creds the dispatch degrades to Blob, proving
  the fallback end-to-end).
- `uv run ruff check api` → clean.
- Consumer search: every `list_result_blobs` / `list_parseable_result_blobs`
  caller reads only the shared `name` / `file_id` fields — agnostic to the SDK.

## Self-critique (design pass)

- **Contract**: identical row shape; `file_id` uses the same encoder so
  downloads still decode. ✓
- **Liveness**: `get_paths` iteration bounded by `limit`. ✓
- **Partial failure**: dfs error → Blob fallback (logged); missing dir → `[]`. ✓
- **Security**: no SAS; prefix is the normalized resolved prefix; scoped to the
  filesystem. ✓
- **Backward-compat**: flag OFF = Blob (unchanged). ✓
- **Medium note**: `last_modified` is `datetime.isoformat()` from both SDKs in
  practice; the string passthrough branch is defensive and only affects
  display/sort. Documented.
- Verdict: no Critical/High.
