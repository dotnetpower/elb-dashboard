# 2026-05-23 — services/openapi subpackage

## Motivation
The `api/services/` directory has 50+ flat files with `openapi_`, `blast_`, `k8s_`,
`warmup_`, `storage_`, `db_` prefixes that already imply domain grouping. This is
Phase A bundle 1 of a wider SRP / directory cleanup that follows the same shim
pattern already used by `services/k8s_client.py`, `services/k8s_nodes.py`,
`services/blast_task_config.py`, and `services/warmup_task_planning.py`.

## User-facing change
None — internal layout only.

## API / IaC diff summary
- `api/services/openapi_deployment.py` → `api/services/openapi/deployment.py`
- `api/services/openapi_runtime.py` → `api/services/openapi/runtime.py`
- `api/services/openapi_token.py` → `api/services/openapi/token.py`
- New `api/services/openapi/__init__.py` package marker.
- Compatibility shims left at the old flat paths (re-exports with explicit `__all__`).
- All in-repo call sites (routes, tasks, services, tests) rewritten to the new
  canonical paths. Tests that monkey-patch module attributes must target the real
  module, so test imports were updated from `from api.services import openapi_X` to
  `from api.services.openapi import X as openapi_X`.

## Validation
- `uv run pytest -q api/tests` → 1260 passed in 62.59s
- `uv run ruff check api` → All checks passed
