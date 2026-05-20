# BLAST route package SRP split

## Motivation

`api/routes/blast.py` had grown into a multi-thousand-line router that mixed HTTP endpoints with result analytics helpers. The size made route ownership and future changes harder to review.

## User-facing change

No intended API or UI behavior change. `/api/blast/*` keeps the same paths and `blast_router` import surface.

## API / IaC diff summary

- Promoted `api.routes.blast` from a single module to a package.
- Kept `blast_router` exported from `api/routes/blast/__init__.py` so `api/main.py` registration remains stable.
- Moved BLAST result file, analytics, download, and export routes to `api/routes/blast/results.py`.
- Moved pure result analytics helpers to `api/services/blast_result_analytics.py`.
- Updated route documentation and the agent codebase map.
- No IaC changes.

## Validation evidence

- `uv run ruff check api/routes/blast api/services/blast_result_analytics.py`
- `PYTHONPATH=$PWD uv run pytest -q api/tests` — 702 passed.
