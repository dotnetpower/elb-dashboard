# Fix flaky CI: mock validate_blast_database_available in preflight contract test

## Motivation

`api/tests/test_response_contracts.py::test_preflight_returns_admission_decision`
has been failing in the GitHub Actions `Tests` workflow on every push to `main`
since 2026-05-23 (`d67ec27`, `6684d1d`, `b08bd03`). The assertion
`body["ready"] is True` was failing because the `database` pre-flight check
returned `fail`:

```
[pass] aks_cluster: elb-cluster is running (3 nodes)
[pass] storage: elbstg01 configured
[fail] database: Could not verify BLAST database 'core_nt' in Storage: TypeError.
[pass] sharding_precision: full
[pass] web_blast_compatibility: verified_full_database_profile
[pass] broker: Redis is reachable
```

The test was monkey-patching `services.get_credential` to return a plain
`object()`, but the preflight route's database check goes further and calls
`api.services.storage.data._blob_service(credential, ...)`, which constructs a
real `BlobServiceClient` and ultimately raises `TypeError` because `object()`
does not implement the `TokenCredential` protocol. The test pre-dated the
preflight route's `validate_blast_database_available` integration in the form
it has today.

## User-facing change

None. CI-only test fix; no behaviour or API change.

## API/IaC diff summary

- `api/tests/test_response_contracts.py`: add a `monkeypatch.setattr` for
  `api.services.blast_task_config.validate_blast_database_available` that
  returns a synthetic `{container, blob_prefix, marker_blob}` payload. The
  test is a response-contract test (assert on response shape), so stubbing
  the Storage admission check at its public seam is the correct boundary —
  it matches the mocking strategy already used in
  `api/tests/test_blast_database_availability.py`.

No source or infra changes.

## Validation evidence

- `uv run pytest -q api/tests/test_response_contracts.py` → `3 passed in 4.31s`.
- `uv run pytest -q api/tests` → `1457 passed in 38.42s` (no regressions).
- `uv run ruff check api` → `All checks passed!`.
