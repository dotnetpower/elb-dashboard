# _ensure_table — double-checked lock to collapse first-boot herd

## Motivation
Three `_ensure_table` (state_repo, auto_warmup, job_artifacts) used a
plain `set` for the "already ensured" marker. On worker / api cold
start, concurrent first requests all saw an empty marker and each
fired their own `TableServiceClient` open + `create_table_if_not_exists`
call. The Azure side is idempotent, but each call paid a full TLS
handshake + ARM round-trip; under simultaneous Celery worker boot
across multiple Container App replicas this added measurable startup
latency.

## User-facing change
None. Same idempotent behaviour; only the first caller per
`(endpoint, table)` pays the round-trip.

## API / IaC diff
* `api/services/state_repo.py`,
  `api/services/auto_warmup.py`,
  `api/services/job_artifacts.py`
  * Added `_ENSURED_TABLES_LOCK = threading.Lock()` next to each
    `_ENSURED_TABLES` set.
  * `_ensure_table*` now uses double-checked locking: cheap unlocked
    set membership check first; lock + re-check + actual creation only
    when the marker is missing.

## Validation
* `uv run pytest -q api/tests/test_state_repo.py
  api/tests/test_auto_warmup.py api/tests/test_job_artifacts.py` —
  32 passed.
* `uv run ruff check` on all three modules — clean.
