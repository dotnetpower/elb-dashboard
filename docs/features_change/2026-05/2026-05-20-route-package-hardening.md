# Route package hardening

## Motivation

Several FastAPI route modules had grown beyond a comfortable review size. The first BLAST split also showed that route order and monkeypatch/import compatibility needed explicit tests before continuing the package refactor.

## User-facing change

No intended API or UI behavior change. Existing `/api/blast/*` and `/api/monitor/*` paths remain registered in the same public surface.

## API / IaC diff summary

- Added route contract tests for `/api/blast` import compatibility, result route ordering, and API-before-frontend catch-all registration.
- Split `api/routes/blast/__init__.py` into focused submodules: `jobs.py`, `submit.py`, `databases.py`, `taxonomy.py`, `schedules.py`, and `results.py`.
- Moved BLAST submit body normalization into `api/services/blast_submit_payload.py` while keeping `_blast_shared.py` re-exports for existing tests/imports.
- Promoted `api/routes/monitor.py` into the `api/routes/monitor/` package with focused AKS, metrics, storage, ACR, terminal, cluster, jobs, sidecars, and common modules.
- Kept `api.routes.monitor._SIDECAR_BROADCASTER`, `_SidecarBroadcaster`, `collect_snapshot`, `get_credential`, and `_graceful` re-exported for lifespan hooks and existing tests.
- No IaC changes.

## Validation evidence

- `uv run ruff check api`
- `PYTHONPATH=$PWD uv run pytest -q api/tests` — 705 passed.
