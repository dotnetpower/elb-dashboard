# 2026-05-23 — services/storage subpackage

## Motivation
Continue Phase A of the SRP / directory-cleanup plan. Six `storage_*.py` flat
files now live under a single `services/storage/` namespace, matching the
pattern already in use for `services/k8s/`, `services/blast/` (partially), and
`services/warmup/`.

## User-facing change
None — internal layout only.

## API / IaC diff summary
- `api/services/storage_data.py` → `api/services/storage/data.py`
- `api/services/storage_endpoint.py` → `api/services/storage/endpoint.py`
- `api/services/storage_network.py` → `api/services/storage/network.py`
- `api/services/storage_public_access.py` → `api/services/storage/public_access.py`
- `api/services/storage_url_validation.py` → `api/services/storage/url_validation.py`
- `api/services/storage_usage_cache.py` → `api/services/storage/usage_cache.py`
- New `api/services/storage/__init__.py` package marker.
- Compatibility shims at the old flat paths with explicit `__all__` re-exports.
- All in-repo call sites (services, routes, tasks, tests) and string-based
  `monkeypatch.setattr("api.services.storage_X.Y", ...)` targets rewritten to the
  new canonical paths.
- Combined imports like `from api.services import get_credential, storage_data`
  split so the storage piece resolves to the real module (the shim cannot
  forward attribute monkey-patches into the moved implementation).
- `test_security_audit_bundle.py::test_caller_ip_lookup_url_must_be_https`
  updated to evict the real module (`api.services.storage.public_access`)
  before re-import; popping only the shim left a cached real module so the
  env-var validation never re-ran.

## Validation
- `uv run pytest -q api/tests` → 1260 passed in 60.21s
- `uv run ruff check api` → All checks passed
