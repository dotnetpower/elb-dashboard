---
title: Two robustness fixes — gzip result cap and NCBI signature sampling divisor
description: Capped gzip result decompression so the final flush cannot exceed max_bytes, and guarded the NCBI shard-signature sampler against a divide-by-zero when the sample count is 1.
tags:
  - operate
---

# Gzip result cap + NCBI signature sampling divisor (2026-06-07)

Two provable latent defects found during an E2E backend audit.

## Motivation

### 1. Gzip result decompression could exceed `max_bytes`

`api/services/storage/blob_io.py::read_result_blob_text` streams a `.out.gz`
BLAST result and is contracted to return at most `max_bytes` of decompressed
text (so an analytics route running in the request thread stays bounded).
The chunk loop respected the cap, but the final
`inflater.flush(max_bytes - total)` did not: `zlib.Decompress.flush(length)`
treats `length` as the **initial output-buffer size**, not a hard limit, so it
returns *all* remaining decompressed output — including anything the bounded
`decompress(..., remaining)` calls left in `unconsumed_tail`. A highly
compressible blob (e.g. 1 MiB of one repeated byte → a few hundred compressed
bytes arriving in a single chunk) could therefore return far more than
`max_bytes`, risking memory blow-up under concurrent requests.

### 2. NCBI shard-signature sampler divide-by-zero

`api/services/ncbi_catalogue.py::_pick_signature_keys` evenly samples
`_SIGNATURE_SAMPLE_COUNT` (env `NCBI_SIGNATURE_SAMPLE_COUNT`, default 8)
`.tar.gz.md5` shard files via `step = (len - 1) / (n - 1)`. With the default
this is safe, but `NCBI_SIGNATURE_SAMPLE_COUNT=1` on a multi-shard DB yields
`n == 1` and `len > 1`, so the `len(md5s) <= n` early-return is skipped and the
`(n - 1)` divisor is zero — a `ZeroDivisionError` that crashes
`preview_database()` for that DB.

## User-facing change

None for default configuration. The gzip fix only tightens an upper bound that
was already supposed to hold; the NCBI fix only affects the non-default
`NCBI_SIGNATURE_SAMPLE_COUNT=1` setting, which now returns the first shard key
deterministically instead of crashing.

## API / IaC diff summary

- `api/services/storage/blob_io.py` — `read_result_blob_text`: call
  `inflater.flush()` and slice the result to `max_bytes - total` so the
  decompressed payload can never exceed the cap.
- `api/services/ncbi_catalogue.py` — `_pick_signature_keys`: add an `if n == 1:
  return md5s[:1]` guard before the evenly-spaced computation.
- No IaC change.

## Validation evidence

- `uv run pytest -q api/tests/test_storage_data.py api/tests/test_ncbi_catalogue.py` — 43 passed, including the new
  `test_read_result_blob_text_gzip_flush_respects_cap` (1 MiB single-byte blob
  capped to 256 bytes) and
  `test_pick_signature_keys_sample_count_one_does_not_divide_by_zero` /
  `test_pick_signature_keys_even_spacing_includes_first_and_last`.
- `uv run ruff check api/services/storage/blob_io.py api/services/ncbi_catalogue.py` — clean.
