# Bound every metadata `download_blob().readall()` with a hard size cap

## Motivation
`download_blob().readall()` without `length=` is unbounded — a corrupt or
maliciously oversized blob (BLAST `.njs`, `*-metadata.json`, oracle
`status.json`, manifest/`.nal`, upgrade history/build logs) would OOM the
api/worker sidecar.

## User-facing change
None. Same blobs read by the same code paths; the cap only fires when a
blob exceeds the configured maximum (16 MiB default, 4 MiB for the small
`*-metadata.json` / `status.json` / shard-manifest blobs, 64 KiB for
`.nal`/`.manifest` equality probes). When the cap is hit the helper logs a
warning and raises `ValueError`; existing callers already wrap reads in
`except Exception` and degrade.

## API / IaC diff
* `api/services/storage_data.py` adds `METADATA_BLOB_MAX_BYTES = 16 MiB`,
  `read_metadata_blob_bytes(blob_client, *, max_bytes, label)`, and
  `read_metadata_blob_text(...)`. The helper requests `length=max_bytes+1`
  server-side and rejects the blob if more than `max_bytes` come back.
* All 10 unbounded `readall()` sites routed through the helper:
  * `api/services/storage_data.py` — `*-metadata.json`, oracle
    `status.json`, BLAST `.njs` (list view).
  * `api/services/db_sharding.py` — full-DB `.njs` stats + idempotency
    manifest/`.nal` comparison (64 KiB cap).
  * `api/services/blast_oracles.py` — oracle status JSON.
  * `api/services/blast_db_metadata.py` — display metadata + `.njs` lookup.
  * `api/routes/blast/databases.py` — start-sharding read.
  * `api/routes/storage/prepare_db.py` — `_read_db_metadata` +
    `_download_blob_with_etag` (inline cap because the route also needs
    the stream `.properties.etag`).
  * `api/tasks/storage/warmup.py` — pre/final/error metadata reads.
  * `api/services/upgrade/history.py` — upgrade history append-blob.
  * `api/services/upgrade/build_logs.py` — per-image build log.
* Test fakes in `test_db_sharding.py`, `test_blast_oracles.py`,
  `test_storage_data.py`, `test_prepare_db_routes.py`,
  `test_prepare_db_hardening.py` updated to accept the new
  `offset` / `length` kwargs on `download_blob`.

## Validation
* `uv run pytest -q api/tests/test_storage_data.py api/tests/test_db_sharding.py
  api/tests/test_blast_oracles.py api/tests/test_blast_db_metadata.py
  api/tests/test_prepare_db_routes.py api/tests/test_prepare_db_hardening.py`
  — 105 passed.
* `uv run ruff check` on all changed files — clean.
