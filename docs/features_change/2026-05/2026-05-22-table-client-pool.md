# Pool the per-request TableClient in job_artifacts + auto_warmup

## Motivation
`api/services/job_artifacts.py::_artifact_table_client` and
`api/services/auto_warmup.py::_table_client` returned a freshly-built
`TableClient` on every call, and callers wrapped the returned client in
a `with` block. The `with` exit closes the underlying azure-core HTTP
pipeline, so each artifact write / preference lookup paid one full TLS
handshake. `state_repo` had already solved this for the jobstate /
jobhistory tables via `_PooledTableClient`; this commit applies the same
pattern to the remaining two callers.

## User-facing change
None. Same behavior, lower latency + bounded FD usage on the table
control plane.

## API / IaC diff
* `api/services/job_artifacts.py`
  * Module-level `_ARTIFACT_TABLE_POOLED` cache + lock.
  * `_artifact_table_client()` returns a process-shared
    `_PooledTableClient(TableClient(...))` so `with table:` no longer
    closes the underlying HTTP pipeline.
  * `_reset_artifact_table_pool()` test hook.
* `api/services/auto_warmup.py`
  * Same pattern for the `autowarmup` table.
  * `_reset_autowarmup_table_pool()` test hook.
* `api/conftest.py`
  * Autouse cleanup fixture calls both new reset hooks so per-test
    monkeypatches on `get_credential` / `TableClient` don't leak through
    the pooled wrapper.

## Validation
* `uv run pytest -q api/tests/test_job_artifacts.py
  api/tests/test_auto_warmup.py api/tests/test_smoke.py` — 97 passed.
* `uv run ruff check api/services/job_artifacts.py
  api/services/auto_warmup.py api/conftest.py` — clean.
