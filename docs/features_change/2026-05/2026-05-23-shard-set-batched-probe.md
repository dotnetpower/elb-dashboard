# _shard_set_already_present — single list_blobs probe

## Motivation
The shard-layout idempotency probe issued one `get_blob_properties()`
HEAD call per shard. A 100-shard layout paid 100 sequential HTTPS round
trips on every `prepare-db` / `ensure_shard_sets` call. The Azure SDK
caps the connection pool so even with HTTP keep-alive the total wall
time on a cold cache was N × RTT.

## User-facing change
None functionally. `prepare-db` start time drops to a single
`list_blobs(name_starts_with=…)` for layouts that already exist.

## API / IaC diff
* `api/services/db_sharding.py::_shard_set_already_present`
  * Probe via one `cc.list_blobs(name_starts_with="{N}shards/")` and
    intersect with the expected `.nal` paths.
  * Falls back to the original per-blob HEAD path if `list_blobs`
    itself raises (some Azure SDK auth edge cases historically only
    surface on enumerate).
* `api/tests/test_db_sharding.py::_FakeContainerClient.list_blobs`
  * Test fake now surfaces both the explicit `_blobs` list and the
    `_store` keys so the new batched probe sees what the legacy HEAD
    probe used to see.

## Validation
* `uv run pytest -q api/tests/test_db_sharding.py` — 45 passed.
* `uv run ruff check api/services/db_sharding.py` — clean.
