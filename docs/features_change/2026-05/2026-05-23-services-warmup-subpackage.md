# 2026-05-23 — services/warmup subpackage (jobs/planner/scripts)

## Motivation
Continue Phase A: fold the remaining flat `warmup_*.py` files into the existing
`services/warmup/` subpackage (which previously held only `task_planning.py`).

## User-facing change
None.

## Diff
- `api/services/warmup_jobs.py` → `api/services/warmup/jobs.py`
- `api/services/warmup_planner.py` → `api/services/warmup/planner.py`
- `api/services/warmup_scripts.py` → `api/services/warmup/scripts.py`
- Compatibility shims at the legacy paths with explicit `__all__`.
- All in-repo call sites + monkey-patch strings updated.

## Validation
- `uv run pytest -q api/tests` → 1260 passed
- `uv run ruff check api` → All checks passed
