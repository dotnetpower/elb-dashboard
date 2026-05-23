# API refactor compatibility hardening

## Motivation

Recent API refactors split several large service modules into focused subpackages. The split left compatibility gaps where legacy flat import paths and monkeypatch surfaces no longer reached the new implementation modules.

## User-facing change

No visible UI workflow changes. The API now preserves the pre-refactor compatibility contracts for Storage, Kubernetes, taxonomy, and job-state helper imports, reducing runtime failures after deployment or local test runs.

## API/IaC diff summary

- Added flat compatibility shims for newly split Kubernetes and Storage submodules.
- Restored patch forwarding for `api.services.storage.data` and `api.services.state_repo` so legacy callers and tests reach the split implementations.
- Kept task ownership status routes fail-closed outside explicit `AUTH_DEV_BYPASS=true`.
- Avoided optional external BLAST detail calls on unscoped job lists while preserving scoped enrichment.
- No IaC changes.

## Validation evidence

- `uv run ruff check api` passed.
- `uv run pytest -q api/tests/test_services_facade_contract.py api/tests/test_tasks_facade_contract.py api/tests/test_taxonomy_search.py api/tests/test_taxonomy_detail.py api/tests/test_taxonomy_tree.py api/tests/test_k8s_blast_status.py api/tests/test_k8s_list_events.py api/tests/test_k8s_warmup_status_parallel.py` passed: 208 tests.
- `uv run pytest -q api/tests` passed: 1369 tests.