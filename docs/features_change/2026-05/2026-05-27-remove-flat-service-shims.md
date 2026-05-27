# Remove flat `api/services/*_*.py` compatibility shims

## Motivation

Phase A of the service-layer SRP refactor left 51 flat re-export modules under
`api/services/` (e.g. `blast_task_config.py`, `storage_data.py`) that simply
forwarded to the canonical subpackage path (`api.services.blast.task_config`,
`api.services.storage.data`, …). Of those 51, 44 had zero remaining callers
and the other 7 had a small handful of imports plus three monkeypatch string
targets in tests. The shims existed only because the
`test_services_facade_contract.py` guard test enforced their continued
presence, which in turn discouraged removing them. This change cuts the dead
weight in one pass.

## User-facing change

None. Pure refactor; runtime behaviour is unchanged.

## API / IaC diff summary

- Deleted **51** compatibility shim files under `api/services/`:
  - `blast_*.py` (17), `db_*.py` (3), `k8s_*.py` (10), `openapi_*.py` (3),
    `storage_*.py` (15), `warmup_*.py` (4).
- Deleted `api/tests/test_services_facade_contract.py` (its sole purpose was
  to guard the shims that no longer exist).
- Migrated 14 callers to the canonical subpackage path
  (`api.services.<prefix>.<name>`):
  - `api/_http_utils.py`, `api/routes/blast/preflight.py`,
    `api/services/__init__.py` (`reset_credential` lazy import tuple),
    `api/services/storage/usage_cache.py` (docstring reference),
    `api/tasks/blast/__init__.py`, `api/tasks/blast/config_shims.py`
    (docstring + imports), `api/tasks/blast/submit_task.py`,
    `api/tasks/storage/__init__.py`,
    `api/tests/test_blast_config_sharding.py` (docstring),
    `api/tests/test_blast_database_availability.py` (import + monkeypatch
    string), `api/tests/test_blast_results_parser.py` (docstring),
    `api/tests/test_response_contracts.py` (two monkeypatch strings),
    `api/tests/test_security_audit_bundle.py` (`sys.modules.pop` calls),
    `api/tests/test_storage_public_access.py` (docstring).
- No production code path or HTTP contract changed.
- Historical change notes under `docs/features_change/` and the planning
  doc `docs/research/web-blast-compatibility-plan.md` keep their original
  flat-path references — they are a historical record, not live
  documentation, so they were not rewritten.
- Live agent-facing surfaces had stale comment/link references to the
  removed flat paths and were updated to the canonical subpackage paths
  in a second pass: `.github/copilot-instructions.md` §9,
  [AGENTS.md](../../../AGENTS.md), [docs/copilot/codebase-map.md](../../copilot/codebase-map.md)
  (rows + dropped "Compatibility wrapper:" suffixes), plus inline
  comments in `api/routes/storage/prepare_db.py`,
  `api/services/k8s/timestamps.py`, `api/services/ncbi_catalogue.py`,
  `api/services/warmup/scripts.py`, `web/src/api/blast.ts`,
  `web/src/api/storage.ts`, `web/src/utils/dbSharding.ts`,
  `web/src/pages/blastSubmit/ComputeSection.tsx`,
  `web/src/pages/blastResults/analytics/helpers.ts`,
  `scripts/dev/README.md`, `scripts/dev/grant-local-rbac.sh`.

## Validation evidence

- `uv run pytest -q api/tests` → **1491 passed in 34.52s**.
- `uv run ruff check api` → **All checks passed!**
- `cd web && npx tsc --noEmit` → clean.
- `uv run mkdocs build --clean --strict` → success.
- `grep -rn 'api[/.]services[/.](blast|db|k8s|openapi|storage|warmup)_'`
  across the repo (excluding `.venv`, `node_modules`, `site/`,
  `.pytest_cache/`, `.logs/`, `test-results/`, `dist/`, the
  `docs/features_change/` historical archive, `docs/research/web-blast-compatibility-plan.md`,
  and built mock-app source maps) returns **zero** matches.
- `uv run python -c "import api.services; import api.services.storage.data as sd; api.services.reset_credential()"`
  → confirmed `reset_blob_service_pool` resolves at the canonical
  `api.services.storage.data` path (it is in that module's `__all__`,
  forwarded from `api.services.storage.client_pool`).
