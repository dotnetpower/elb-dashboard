# reset_credential() cascade — reset every downstream pool

## Motivation
`reset_credential()` only cleared `_BLOB_SERVICE_POOL`. After this
session's hardening work three more pools also hold references to the
credential or its derived clients:

* `api.services.job_artifacts._ARTIFACT_TABLE_POOLED`
* `api.services.auto_warmup._AUTOWARMUP_TABLE_POOLED`
* `api.services.redis_clients._CLIENTS` (used by submit_lock,
  blast_db_metadata pub/sub, openapi_runtime, auto_warmup_reconcile,
  event_emitter via shared keys, sidecar_metrics, health)

Without the cascade a test (or a hypothetical credential rotation)
would leave those pools holding stale references to a dead credential.

## User-facing change
None. Test isolation strengthened; production never calls
`reset_credential()`.

## API / IaC diff
* `api/services/__init__.py::reset_credential` now iterates a small
  module/attr table and invokes every downstream reset hook. Each call
  is wrapped in its own try so a missing dep (or import error) cannot
  block the credential rotation.

## Validation
* `uv run pytest -q api/tests/test_auth_caching.py
  api/tests/test_storage_data.py api/tests/test_redis_clients.py
  api/tests/test_job_artifacts.py api/tests/test_auto_warmup.py` —
  68 passed.
* `uv run pytest -q api/tests` — 1242 passed.
* `uv run ruff check api/services/__init__.py` — clean.
