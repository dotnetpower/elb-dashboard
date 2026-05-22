# BlobServiceClient pool — finalizer + idle TTL

## Motivation
`_BLOB_SERVICE_POOL` keyed entries by `(id(credential), account_name)`.
That worked while the credential lived but had two latent risks:

* `id()` is recycled after Python GC, so if a credential died while its
  pool entry was still cached and a brand-new credential was assigned the
  same id, the next `_blob_service(new_cred, …)` would return the dead
  credential's BlobServiceClient — silently authenticating with a stale
  token cache.
* Clients held forever. With one account in production the pool never
  evicted, so a transient credential reset that did NOT call
  `reset_blob_service_pool()` would leak that client + its HTTP pool.

## User-facing change
None. The pool still services the steady-state hot path with a cached
`BlobServiceClient`; behaviour change is invisible unless a credential is
collected (then its clients are evicted before the id can be reused) or
the new `prune_idle_blob_service_clients()` helper is called.

## API / IaC diff
* `api/services/storage_data.py`
  * Each pool entry now stores `(BlobServiceClient, last_used_monotonic)`.
    `_blob_service` updates `last_used` on every hit so an idle sweep
    can prune accurately.
  * `_ensure_credential_eviction(credential)` registers a
    `weakref.finalize` callback that evicts every pooled client matching
    that credential's id the moment the credential is GC'd. A
    `_BLOB_SERVICE_CREDENTIAL_FINALIZED` set guards against double
    registration so the per-cred overhead is one weakref total.
  * `prune_idle_blob_service_clients(*, idle_ttl_seconds)` (default
    1800 s, overridable via `BLOB_SERVICE_POOL_IDLE_TTL_SECONDS`)
    closes pooled clients whose `last_used` is older than the TTL.
  * `reset_blob_service_pool` updated to also clear
    `_BLOB_SERVICE_CREDENTIAL_FINALIZED` so the next access re-registers
    the finalizer against the current credential.

## Validation
* `uv run pytest -q api/tests/test_storage_data.py
  api/tests/test_db_sharding.py api/tests/test_blast_oracles.py` —
  84 passed (the existing per-credential / per-account pool semantics
  tests cover the new key shape).
* `uv run ruff check api/services/storage_data.py` — clean.
