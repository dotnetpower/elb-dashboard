# AKS and Storage route packages

## Motivation

`api/routes/aks.py` and `api/routes/storage.py` had grown into mixed-responsibility route modules. The earlier BLAST and monitor package split established a clearer route-package pattern, so AKS and Storage now follow the same structure.

## User-facing change

No endpoint paths, payloads, or response shapes changed. Existing dashboard calls continue to use `/api/aks/*` and `/api/storage/*`.

## API / IaC diff summary

- Split `/api/aks` into `api/routes/aks/` submodules for SKUs, provisioning, OpenAPI deploy/spec/proxy, lifecycle, and role assignment.
- Split `/api/storage` into `api/routes/storage/` submodules for `prepare-db` and local-debug storage firewall helpers.
- Moved BLAST job projection, file preview, external OpenAPI context/cache, and Table sync helpers from `api/routes/_blast_shared.py` to `api/services/blast_job_state.py` while preserving `_blast_shared` re-exports for compatibility.
- Added route contract coverage for AKS and Storage package import surfaces and catch-all ordering.
- Updated route maps in `api/routes/README.md`, `docs/copilot/codebase-map.md`, and `AGENTS.md`.
- No IaC changes.

## Validation evidence

- `PYTHONPATH=$PWD uv run pytest -q api/tests/test_route_contracts.py` -> 5 passed.
- `uv run ruff check api` -> all checks passed.
- `PYTHONPATH=$PWD uv run pytest -q api/tests` -> 707 passed.
- Control-character scan across changed route/service/test/note files returned no matches.
